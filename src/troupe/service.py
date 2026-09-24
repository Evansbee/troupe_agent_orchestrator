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


def write_service_state(
    state_dir: Path, state: str, reason: str = "", restarts: int | None = None
):
    path = state_dir / "service.json"
    if restarts is None:
        try:
            restarts = json.loads(path.read_text()).get("restarts", 0)
        except (OSError, ValueError):
            restarts = 0
    value = dict(state=state, reason=reason, since=time.time(), restarts=restarts)
    fd, name = tempfile.mkstemp(prefix=".service-", dir=state_dir)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)




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
    live = bool(
        type(pid) is int
        and pid > 0
        and _locked(state)
        and record.get("identity")
        and _identity(pid) == record["identity"]
    )
    heartbeat = 0
    if (state / "troupe.db").exists():
        heartbeat = Store(state / "troupe.db").kv_get("heartbeat", 0) or 0
    return dict(
        state="running" if live else "stale" if record else "stopped",
        pid=pid if live else None,
        version=record.get("version", ""),
        started_at=record.get("started_at", 0),
        uptime=max(0, time.time() - record.get("started_at", time.time())),
        heartbeat=heartbeat,
        heartbeat_age=time.time() - heartbeat if heartbeat else None,
    )


def start_service(cfg, timeout: float = 10) -> dict:
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


def stop_service(cfg, timeout: float = 15) -> bool:
    status = service_status(cfg.root)
    if status["state"] != "running":
        if not _locked(cfg.state_dir):
            (cfg.state_dir / "engine.pid").unlink(missing_ok=True)
        return False
    pid = status["pid"]
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _locked(cfg.state_dir):
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

            worker = asyncio.create_task(log_events())
            try:
                logger.info(
                    "Engine started pid=%s version=%s", os.getpid(), __version__
                )
                await eng.main()
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        try:
            asyncio.run(run())
        finally:
            eng.store.kv_set("heartbeat", 0)
            logger.info("Engine stopped")
            logger.removeHandler(handler)
            handler.close()
            (cfg.state_dir / "engine.pid").unlink(missing_ok=True)
            for sig, previous in old_handlers.items():
                signal.signal(sig, previous)
        return True
