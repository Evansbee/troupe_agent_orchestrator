"""Detached per-project services, lifetime ownership, registry and recovery."""

# Lock ownership, rather than the pid file alone, is the authority for a live service.
import asyncio
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .store import HandleBook, Store


def _read_service_json(state_dir: Path) -> dict:
    try:
        return json.loads((state_dir / "service.json").read_text())
    except (OSError, ValueError):
        return {}


def _write_service_json(state_dir: Path, value: dict) -> None:
    path = state_dir / "service.json"
    fd, name = tempfile.mkstemp(prefix=".service-", dir=state_dir)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_service_state(
    state_dir: Path, state: str, reason: str = "", restarts: int | None = None
):
    existing = _read_service_json(state_dir)
    if restarts is None:
        restarts = existing.get("restarts", 0)
    value = dict(state=state, reason=reason, since=time.time(), restarts=restarts)
    if state == "stopped":
        # QA's #100 regression: a stale owner record must never outlive the engine it named, or
        # (a) a later, unrelated engine looks like the same orphaned TUI and gets silently
        # adopted, or (b) a real orphan looks falsely owned because its pid got reused. Every stop
        # path -- stop_service, kill_service_now, and the engine's own clean shutdown (api.py's
        # serve() calling this with "stopped" from its finally block) -- funnels through here.
        value.update(owner_kind=None, owner_pid=None, owner_identity=None)
    else:
        # Otherwise this is the launcher's concern (set_owner, below), not the engine's: this is
        # also called from inside the engine process itself (api.py, state="running" at startup),
        # which must not clobber who started it.
        value.update(owner_kind=existing.get("owner_kind"), owner_pid=existing.get("owner_pid"),
                     owner_identity=existing.get("owner_identity"))
    _write_service_json(state_dir, value)


def set_owner(state_dir: Path, pid: int, kind: str) -> None:
    """Record which launcher process is responsible for this engine, and how (REQ-TUI-001's
    relaunch re-adopt, #100 F3 + QA's regression fix): `kind` is "tui", "up" or "service" (matching
    the caller). Only a "tui" owner is ever a candidate for re-adoption -- `troupe up`/`start`/
    `engine` legitimately keep an engine running independent of their own process lifetime, exactly
    like a plain `troupe start` does, so a later TUI must still just attach to those, not adopt
    them, even if their own launcher process has since exited."""
    assert kind in ("tui", "up", "service"), kind
    existing = _read_service_json(state_dir)
    existing.update(owner_kind=kind, owner_pid=pid, owner_identity=_identity(pid))
    existing.setdefault("since", time.time())
    _write_service_json(state_dir, existing)


def tui_owner_is_dead(root: Path) -> bool:
    """True only when the recorded owner is a TUI and that specific process is provably gone
    (identity-checked, so a pid the kernel later reused for something unrelated is never mistaken
    for the same still-alive TUI). Any other owner ("up", "service", or none recorded at all --
    pre-#100 engines, or `troupe up`/`start`/`engine`, never having named themselves "tui") is
    never a re-adopt candidate; ensure_engine just attaches to those, as before #100."""
    owner = _read_service_json(root / ".troupe")
    if owner.get("owner_kind") != "tui" or owner.get("owner_pid") is None:
        return False
    pid = owner["owner_pid"]
    return not (_process_alive(pid) and _identity(pid) == owner.get("owner_identity"))


def _atomic_json(path: Path, value) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix="." + path.name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def register_project(root: Path, name: str) -> None:
    directory = Path.home() / ".troupe"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "projects.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "projects.json"
        try:
            rows = json.loads(path.read_text())
        except FileNotFoundError:
            rows = []
        rows = [r for r in rows if r["path"] != str(root.resolve())]
        rows.append(dict(name=name, path=str(root.resolve()), last_opened=time.time()))
        _atomic_json(path, rows)


def projects() -> list[dict]:
    try:
        rows = json.loads((Path.home() / ".troupe/projects.json").read_text())
    except FileNotFoundError:
        return []
    return [dict(r, **service_status(Path(r["path"]))) for r in rows]


def _identity(pid: int) -> str:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "args="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return ""


def _locked(state: Path) -> bool:
    with (state / "engine.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours
    return True


def service_status(root: Path) -> dict:
    state = root / ".troupe"
    if not state.is_dir():
        return dict(state="missing", pid=None)
    try:
        record = json.loads((state / "engine.pid").read_text())
        if not isinstance(record, dict):
            record = {"pid": int(record)}
    except (OSError, ValueError, TypeError):
        record = {}
    pid = record.get("pid")
    heartbeat = 0
    if (state / "troupe.db").exists():
        heartbeat = Store(state / "troupe.db").kv_get("heartbeat", 0) or 0
    heartbeat_age = time.time() - heartbeat if heartbeat else None
    live = bool(
        type(pid) is int
        and pid > 0
        and _locked(state)
        and record.get("identity")
        and _identity(pid) == record["identity"]
    )
    # Pre-#24 builds wrote a bare integer and never flocked engine.lock, so they're invisible to
    # the check above. Recognize them by pid liveness + a fresh heartbeat (REQ-ENG-005) instead,
    # so `troupe up` attaches to them rather than starting a second engine (REQ-ENG-003).
    legacy = bool(
        not live
        and record
        and "identity" not in record
        and type(pid) is int
        and pid > 0
        and heartbeat_age is not None
        and heartbeat_age < 5
        and _process_alive(pid)
    )
    live = live or legacy
    return dict(
        state="running" if live else "stale" if record else "stopped",
        pid=pid if live else None,
        legacy=legacy,
        version=record.get("version", ""),
        started_at=record.get("started_at", 0),
        uptime=max(0, time.time() - record.get("started_at", time.time())),
        heartbeat=heartbeat,
        heartbeat_age=heartbeat_age,
        owner_pid=_read_service_json(state).get("owner_pid") if live else None,
        owner_kind=_read_service_json(state).get("owner_kind") if live else None,
    )


def start_service(cfg, timeout: float = 10, owner: str = "service") -> dict:
    """`owner` records who's responsible for this engine ("tui", "up" or "service") -- but only if
    THIS call is the one that actually spawns it. Attaching to an engine someone else already
    started must never overwrite (or invent) an ownership record for it; that's what QA's #100
    regression was -- a `troupe start`-owned engine had no owner recorded, which ensure_engine's
    old adopt-when-ownerless rule misread as an orphan and adopted."""
    register_project(cfg.root, cfg.project)
    status = service_status(cfg.root)
    child = None
    if status["state"] != "running":
        # Children contend for the lifetime lock; losers never touch the winner's pid.
        with (cfg.state_dir / "engine.log").open("ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "troupe.cli", "engine"], cwd=cfg.root,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True,
            )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service_status(cfg.root)
        if (
            status["state"] == "running"
            and status["heartbeat_age"] is not None
            and status["heartbeat_age"] < 2
            and status["heartbeat"] >= status["started_at"]
        ):
            if child is not None:
                set_owner(cfg.state_dir, os.getpid(), owner)
            return status
        if child is not None and child.poll() not in (None, 0):
            break
        time.sleep(0.05)
    tail = (cfg.state_dir / "engine.log").read_text(errors="replace").splitlines()[-30:]
    raise RuntimeError(
        "Engine did not become ready within 10 seconds.\n" + "\n".join(tail)
    )


def recover_interrupted(store: Store) -> None:
    for run in store.q("SELECT id FROM runs WHERE status='running'"):
        key = f"run_mail.{run['id']}"
        store.mark_unread(store.kv_get(key, []))
        store.x("DELETE FROM kv WHERE key=?", key)
    store.x(
        "UPDATE runs SET status='interrupted', ended=? WHERE status='running'",
        time.time(),
    )
    store.x(
        "UPDATE agents SET state='idle',current_run=NULL,activity='' WHERE state='running'"
    )


def kill_descendants(pid: int) -> None:
    rows = subprocess.check_output(["ps", "-axo", "pid=,ppid="], text=True).splitlines()
    parents = {
        int(row.split()[0]): int(row.split()[1])
        for row in rows
        if len(row.split()) == 2
    }
    descendants = {pid}
    while True:
        more = {child for child, parent in parents.items() if parent in descendants}
        if more <= descendants:
            break
        descendants |= more
    descendants.discard(pid)
    identities = {child: _identity(child) for child in descendants}
    for child, identity in identities.items():
        if identity and _identity(child) == identity:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _stop_legacy(cfg, pid: int, timeout: float) -> bool:
    """Pre-#24 engines hold no lock, so wait for the heartbeat to go stale instead of the lock."""
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if service_status(cfg.root)["state"] != "running":
            break
        time.sleep(0.05)
    else:
        if _process_alive(pid):
            os.kill(pid, signal.SIGKILL)
    recover_interrupted(Store(cfg.db_path))
    (cfg.state_dir / "engine.pid").unlink(missing_ok=True)
    write_service_state(cfg.state_dir, "stopped", "Stopped legacy engine")
    return True


def stop_service(cfg, timeout: float = 15) -> bool:
    status = service_status(cfg.root)
    if status["state"] != "running":
        if not _locked(cfg.state_dir):
            (cfg.state_dir / "engine.pid").unlink(missing_ok=True)
        return False
    pid = status["pid"]
    if status["legacy"]:
        return _stop_legacy(cfg, pid, timeout)
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _locked(cfg.state_dir):
            # #113: the engine's own shutdown (run_foreground's finally) should already have
            # written "stopped" by the time it releases this lock, but writing it here too means
            # stop_service's own success is never dependent on that timing having landed first.
            write_service_state(cfg.state_dir, "stopped", "Stopped")
            return True
        time.sleep(0.05)
    # Recheck identity before signalling: a recycled pid is never a valid stop target.
    if service_status(cfg.root).get("pid") == pid:
        kill_descendants(pid)
        os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 2
    while _locked(cfg.state_dir) and time.monotonic() < deadline:
        time.sleep(0.02)
    if not _locked(cfg.state_dir):
        recover_interrupted(Store(cfg.db_path))
        (cfg.state_dir / "engine.pid").unlink(missing_ok=True)
        write_service_state(cfg.state_dir, "stopped", "Stopped after shutdown timeout")
    else:
        raise RuntimeError("Engine did not stop; lifetime lock is still held")
    return True


def kill_service_now(cfg) -> bool:
    """No SIGTERM grace period: REQ-TUI-001's "a second Ctrl-C SIGKILLs any remaining runs" — the
    human already asked once and is done waiting."""
    status = service_status(cfg.root)
    if status["state"] != "running":
        if not _locked(cfg.state_dir):
            (cfg.state_dir / "engine.pid").unlink(missing_ok=True)
        return False
    pid = status["pid"]
    kill_descendants(pid)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 2
    while _locked(cfg.state_dir) and time.monotonic() < deadline:
        time.sleep(0.02)
    if not _locked(cfg.state_dir):
        recover_interrupted(Store(cfg.db_path))
        (cfg.state_dir / "engine.pid").unlink(missing_ok=True)
        write_service_state(cfg.state_dir, "stopped", "Force-killed (second Ctrl-C)")
    return True


def run_foreground(cfg) -> bool:
    from .engine import Engine

    with (cfg.state_dir / "engine.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        record = dict(
            pid=os.getpid(),
            started_at=time.time(),
            version=__version__,
            identity=_identity(os.getpid()),
        )
        _atomic_json(cfg.state_dir / "engine.pid", record)
        # #113: self-register as our own "service" owner immediately -- a standalone `troupe
        # engine` has no launcher to ever call set_owner for it, so without this it keeps
        # whatever owner happened to be recorded from a previous, unrelated run (e.g. a dead
        # TUI's stale "tui" record), which a later `ensure_engine` would misread as an orphan to
        # re-adopt -- and then kill on its own Ctrl-C. `start_service`'s own post-heartbeat
        # set_owner call (tui/up/service, with the *launcher's* pid) always lands after this and
        # overwrites it when this process actually is that kind of spawn.
        set_owner(cfg.state_dir, os.getpid(), "service")
        handler = RotatingFileHandler(
            cfg.state_dir / "engine.log", maxBytes=10 * 1024 * 1024, backupCount=3
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger = logging.getLogger(f"troupe.service.{os.getpid()}")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False
        eng = Engine(cfg)
        old_handlers = {
            sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)
        }
        for sig in old_handlers:
            signal.signal(sig, lambda *_: eng.stop())

        async def run():
            cursor = eng.store.max_event_id()

            async def log_events():
                nonlocal cursor
                while True:
                    names = HandleBook(eng.cfg.project, eng.cfg.agents)
                    for event in reversed(eng.store.events(limit=10000, after=cursor)):
                        cursor = max(cursor, event["id"])
                        logger.info(
                            "%s %s",
                            names.name(event["agent"]),
                            names.event_text(event["text"]),
                        )
                    await asyncio.sleep(0.2)

            async def watch_liveness():
                # #103: this process is detached (start_new_session=True in start_service), so
                # nothing else notices if what it's serving disappears out from under it — the
                # exact way orphaned test engines outlived their already-deleted /tmp project dirs
                # and kept burning CPU/RAM on the human's laptop. Self-exit is the backstop: no
                # matter *why* the root vanished (a crashed test's tmpdir cleanup, the human
                # deleting a project, a stray `rm -rf`), the engine notices within a couple of
                # ticks instead of running forever. TROUPE_EXIT_WITH_PARENT_PID (set only by the
                # test fixture, never in production) adds a second, tighter trigger: exit as soon
                # as the process that spawned this engine is gone, rather than waiting for its
                # directory to vanish too.
                parent_pid_raw = os.environ.get("TROUPE_EXIT_WITH_PARENT_PID", "")
                parent_pid = int(parent_pid_raw) if parent_pid_raw.isdigit() else None
                while True:
                    if not cfg.root.is_dir() or not cfg.state_dir.is_dir():
                        logger.info("Project root %s disappeared; stopping.", cfg.root)
                        eng.stop()
                        return
                    if parent_pid is not None and not _process_alive(parent_pid):
                        logger.info("Parent process %s is gone; stopping.", parent_pid)
                        eng.stop()
                        return
                    await asyncio.sleep(2)

            worker = asyncio.create_task(log_events())
            liveness = asyncio.create_task(watch_liveness())
            try:
                logger.info(
                    "Engine started pid=%s version=%s", os.getpid(), __version__
                )
                await eng.main()
            finally:
                worker.cancel()
                liveness.cancel()
                await asyncio.gather(worker, liveness, return_exceptions=True)

        try:
            asyncio.run(run())
        finally:
            # #100: this must be the very first thing that happens, unconditionally -- the pid
            # file's absence has to be a guarantee callers (stop_service's graceful-return path,
            # which trusts we've already done this before it observes the lock released) can rely
            # on regardless of what else in this block does. Under load, a later step here (an
            # SQLite write, a logging call) can be slow enough that stop_service's own timeout
            # expires and escalates to SIGKILL before reaching this line, or can itself raise and
            # abort the rest of the block -- either way, unlinking last made the guarantee only
            # probabilistic instead of ordered.
            (cfg.state_dir / "engine.pid").unlink(missing_ok=True)
            # #113: unconditional on every exit path (SIGTERM, SIGINT, a crash out of run()).
            # api.py's own serve() finally already writes "stopped" and clears the owner, but that
            # runs on the API thread's own event loop with only a bounded join (api.stop()) -- if
            # it hasn't landed by the time this process is on its way out, service.json would
            # otherwise keep showing "running" plus a now-stale owner forever (#113: a TUI's
            # Ctrl-C left exactly that behind, and a later `ensure_engine` adopted and killed an
            # unrelated foreground engine because of it).
            write_service_state(cfg.state_dir, "stopped", "Engine process exited")
            try:
                eng.store.kv_set("heartbeat", 0)
            except Exception:
                pass
            logger.info("Engine stopped")
            logger.removeHandler(handler)
            handler.close()
            for sig, previous in old_handlers.items():
                signal.signal(sig, previous)
        return True
