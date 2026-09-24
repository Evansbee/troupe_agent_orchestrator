"""`troupe` command line."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sys
import time
from pathlib import Path

import httpx

from . import config as config_mod
from . import gitops
from .store import OPEN_STATUSES, Store

PM_GREETING = """\
Hi — I'm your **Product Manager**. 👋

Tell me what we're building. A one-liner is plenty to start; I'll ask questions until we have a crisp \
vision, then the **Spec Writer** turns it into specs, the **Lead** breaks it into tasks, and the \
builders, designer and QA get going. You'll see questions for you pile up in **Needs you** on the right.

What's the idea?"""


def engine_pid_path(cfg: config_mod.Config) -> Path:
    return cfg.state_dir / "engine.pid"


def engine_alive(cfg: config_mod.Config) -> int | None:
    p = engine_pid_path(cfg)
    if not p.exists():
        return None
    try:
        pid = int(p.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        return None


def require_root() -> Path:
    root = config_mod.find_root()
    if not root:
        sys.exit("Not inside a troupe project. Run `troupe init` (or `troupe up`) in your project directory.")
    return root


def cmd_init(args: argparse.Namespace) -> Path:
    root = Path(args.dir).resolve() if getattr(args, "dir", None) else Path.cwd().resolve()
    root.mkdir(parents=True, exist_ok=True)
    cfgfile = root / config_mod.STATE_DIR / config_mod.CONFIG_FILE
    if cfgfile.exists():
        config_mod.load(root)  # migrate a legacy roster even when init is repeated
        print(f"Already initialized: {cfgfile}")
        return root
    name = getattr(args, "name", None) or root.name
    local_model = detect_local_model()
    config_mod.write_default(root, name, local_model)
    gitops.ensure_repo(root)
    cfg = config_mod.load(root)
    store = Store(cfg.db_path)
    store.sync_agents(cfg.agents)
    store.send(next(a.id for a in cfg.agents if a.role == "pm"), "human", PM_GREETING, kind="chat")
    print(f"✓ Initialized troupe in {root}\n  config: {cfgfile}\n  Next: `troupe up`")
    return root


def detect_local_model() -> str | None:
    try:
        r = httpx.get("http://localhost:1234/v1/models", timeout=1.5)
        ids = [m["id"] for m in r.json().get("data", []) if "embed" not in m["id"]]
        return ids[0] if ids else None
    except (httpx.HTTPError, ValueError, KeyError):
        return None


def start_engine(cfg: config_mod.Config):
    from .engine import Engine

    if pid := engine_alive(cfg):
        print(f"Engine already running (pid {pid}); attaching.")
        return None
    engine_pid_path(cfg).write_text(str(os.getpid()))
    eng = Engine(cfg)
    eng.start_thread()
    return eng


def cmd_up(args: argparse.Namespace) -> None:
    root = config_mod.find_root() or cmd_init(args)
    cfg = config_mod.load(root)
    eng = start_engine(cfg)
    try:
        from .gui.app import run_gui
        run_gui(cfg)
    finally:
        if eng:
            eng.stop()
            time.sleep(0.8)
            engine_pid_path(cfg).unlink(missing_ok=True)


def cmd_engine(args: argparse.Namespace) -> None:
    cfg = config_mod.load(require_root())
    eng = start_engine(cfg)
    if not eng:
        return
    print(f"troupe engine running for {cfg.project} — Ctrl-C to stop")
    stop = False

    def handler(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    while not stop:
        time.sleep(0.5)
    eng.stop()
    time.sleep(1.0)
    engine_pid_path(cfg).unlink(missing_ok=True)


def cmd_gui(args: argparse.Namespace) -> None:
    cfg = config_mod.load(require_root())
    if not engine_alive(cfg):
        print("warning: no engine running — start one with `troupe engine`, or use `troupe up`.")
    from .gui.app import run_gui
    run_gui(cfg)


def cmd_status(args: argparse.Namespace) -> None:
    cfg = config_mod.load(require_root())
    s = Store(cfg.db_path)
    pid = engine_alive(cfg)
    print(f"{cfg.project} — engine {'running (pid %d)' % pid if pid else 'stopped'}"
          f"{' · PAUSED' if s.kv_get('paused') else ''}")
    for a in s.agents():
        print(f"  {a['id']:<12} {a['state']:<8} {a['backend']}/{a['model'] or '-':<10} {a['status'][:60]}")
    tasks = s.tasks(OPEN_STATUSES)
    print(f"\n{len(tasks)} open tasks")
    for t in tasks[:20]:
        print(f"  #{t['id']:<4} {t['status']:<12} {t['assignee'] or '-':<10} {t['title'][:70]}")
    qs = s.questions()
    if qs:
        print(f"\n{len(qs)} questions for you:")
        for q in qs:
            print(f"  #{q['id']} ({q['asker']}) {q['question'][:100]}")


def cmd_say(args: argparse.Namespace) -> None:
    cfg = config_mod.load(require_root())
    s = Store(cfg.db_path)
    if not cfg.agent(args.agent):
        sys.exit(f"unknown agent {args.agent!r}")
    s.send("human", args.agent, " ".join(args.text), kind="chat")
    print(f"→ {args.agent}")


def cmd_doctor(args: argparse.Namespace) -> None:
    root = config_mod.find_root()
    cfg = config_mod.load(root) if root else None
    be = cfg.backends if cfg else config_mod.Backends()
    ok = True
    for name, cmd in (("claude", be.claude_command), ("codex", be.codex_command), ("git", "git")):
        path = shutil.which(cmd)
        print(f"  {'✓' if path else '✗'} {name:<7} {path or 'not found'}")
        ok &= bool(path)
    try:
        r = httpx.get(be.local_base_url.rstrip("/") + "/models", timeout=2)
        models = [m["id"] for m in r.json().get("data", [])]
        print(f"  ✓ local   {be.local_base_url} ({len(models)} models: {', '.join(models[:4])})")
    except (httpx.HTTPError, ValueError):
        print(f"  ✗ local   {be.local_base_url} unreachable (only needed for local-backend agents)")
    if cfg:
        print(f"\n  project {cfg.project} at {cfg.root}; engine {'running' if engine_alive(cfg) else 'stopped'}")
    sys.exit(0 if ok else 1)


def main() -> None:
    ap = argparse.ArgumentParser(prog="troupe", description="A team of AI agents that builds software with you.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("init", help="set up a troupe in this directory")
    p.add_argument("--name")
    p.add_argument("dir", nargs="?")
    p = sub.add_parser("up", help="start the engine + GUI (initializes if needed)")
    p.add_argument("--name")
    sub.add_parser("engine", help="run the engine headless")
    sub.add_parser("gui", help="open the GUI against a running engine")
    sub.add_parser("status", help="print team / board / questions")
    p = sub.add_parser("say", help="chat to an agent from the terminal")
    p.add_argument("agent")
    p.add_argument("text", nargs="+")
    sub.add_parser("doctor", help="check backends are available")
    args = ap.parse_args()
    handlers = {"init": cmd_init, "up": cmd_up, "engine": cmd_engine, "gui": cmd_gui, "status": cmd_status,
                "say": cmd_say, "doctor": cmd_doctor}
    handlers.get(args.cmd or "up", cmd_up)(args)
