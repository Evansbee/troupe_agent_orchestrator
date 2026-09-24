"""`troupe` command line."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import httpx

from . import config as config_mod
from . import gitops
from .store import OPEN_STATUSES, Store, HandleBook

PM_GREETING = """\
Hi — I'm your **Product Manager**. 👋

Tell me what we're building. A one-liner is plenty to start; I'll ask questions until we have a crisp \
vision, then the **Spec Writer** turns it into specs, the **Lead** breaks it into tasks, and the \
builders, designer and QA get going. You'll see questions for you pile up in **Needs you** on the right.

What's the idea?"""


def engine_pid_path(cfg: config_mod.Config) -> Path:
    return cfg.state_dir / "engine.pid"


def engine_alive(cfg: config_mod.Config) -> int | None:
    from .service import service_status
    return service_status(cfg.root).get("pid")


def require_root(project: str | None = None) -> Path:
    root = config_mod.find_root(Path(project).resolve() if project else None)
    if not root:
        sys.exit("Not inside a troupe project. Run `troupe init` in your project directory.")
    return root


def cmd_tui(args: argparse.Namespace) -> None:
    import asyncio

    from .tui import run_tui
    from .tui.lifecycle import ensure_engine
    root = require_root(getattr(args, "project", None))
    cfg = config_mod.load(root)
    owns_engine = asyncio.run(ensure_engine(cfg))
    run_tui(cfg, owns_engine=owns_engine)


def cmd_init(args: argparse.Namespace) -> Path:
    root = Path(args.dir).resolve() if getattr(args, "dir", None) else Path.cwd().resolve()
    root.mkdir(parents=True, exist_ok=True)
    from .service import register_project
    cfgfile = root / config_mod.STATE_DIR / config_mod.CONFIG_FILE
    if cfgfile.exists():
        cfg = config_mod.load(root)  # migrate a legacy roster even when init is repeated
        register_project(root, cfg.project)
        print(f"Already initialized: {cfgfile}")
        return root
    name = getattr(args, "name", None) or root.name
    local_model = detect_local_model()
    config_mod.write_default(root, name, local_model)
    gitops.ensure_repo(root)
    cfg = config_mod.load(root)
    register_project(root, cfg.project)
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


def cmd_up(args: argparse.Namespace) -> None:
    from .service import start_service
    root = config_mod.find_root() or cmd_init(args)
    cfg = config_mod.load(root)
    start_service(cfg)
    from .gui.app import run_gui
    run_gui(cfg)


def cmd_engine(args: argparse.Namespace) -> None:
    from .service import run_foreground
    if not run_foreground(config_mod.load(require_root())):
        print("Engine already running; no second engine started.")


def cmd_start(args: argparse.Namespace) -> None:
    from .service import start_service
    status = start_service(config_mod.load(require_root()))
    print(f"Engine running (pid {status['pid']})")


def cmd_stop(args: argparse.Namespace) -> None:
    cfg = config_mod.load(require_root())
    if getattr(args, "now", False):
        from .safety import stop_now
        stop_now(Store(cfg.db_path))
        print("Stopped: no runs, including chat, until you resume.")
        return
    from .service import stop_service
    print("Engine stopped" if stop_service(cfg) else "not running")


def cmd_resume(args: argparse.Namespace) -> None:
    from .safety import resume
    resume(Store(config_mod.load(require_root()).db_path))
    print("Resume requested.")


def cmd_restart(args: argparse.Namespace) -> None:
    cmd_stop(args)
    cmd_start(args)


def cmd_projects(args: argparse.Namespace) -> None:
    from .service import projects
    for project in projects():
        print(f"{project['name']} — {project['state']} — {project['path']}")


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
    names = HandleBook(cfg.project, cfg.agents)
    from .service import service_status
    state = service_status(cfg.root)
    print(f"Service {state['state']} · pid {state.get('pid')} · version {state.get('version', '-')} · "
          f"uptime {state.get('uptime', 0):.0f}s · heartbeat age {state.get('heartbeat_age')}")
    print(f"{cfg.project} — engine {'running (pid %d)' % pid if pid else 'stopped'}"
          f"{' · PAUSED' if s.kv_get('paused') else ''}")
    for a in s.agents():
        print(f"  {names.name(a['id']):<12} {a['state']:<8} {a['backend']}/{a['model'] or '-':<10} {a['status'][:60]}")
    tasks = s.tasks(OPEN_STATUSES)
    print(f"\n{len(tasks)} open tasks")
    for t in tasks[:20]:
        print(f"  #{t['id']:<4} {t['status']:<12} {names.name(t['assignee']):<10} {t['title'][:70]}")
    qs = s.questions()
    if qs:
        print(f"\n{len(qs)} questions for you:")
        for q in qs:
            print(f"  #{q['id']} ({names.name(q['asker'])}) {q['question'][:100]}")

    if not pid:
        raise SystemExit(1)


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


def cmd_api(args: argparse.Namespace) -> None:
    from .api_client import cli
    raise SystemExit(cli(require_root(), args.method, args.params))


def main() -> None:
    ap = argparse.ArgumentParser(prog="troupe", description="A team of AI agents that builds software with you.")
    ap.add_argument("--project", help="target another project's directory instead of the current one")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("init", help="set up a troupe in this directory")
    p.add_argument("--name")
    p.add_argument("dir", nargs="?")
    sub.add_parser("tui", help="open the terminal dashboard (default; starts the engine if needed)")
    p = sub.add_parser("up", help="start the engine + raylib GUI (initializes if needed)")
    p.add_argument("--name")
    sub.add_parser("engine", help="run the engine headless")
    for command in ("start", "restart", "projects", "ps"):
        sub.add_parser(command)
    p = sub.add_parser("stop", help="stop the service, or stop all agent runs immediately with --now")
    p.add_argument("--now", action="store_true")
    sub.add_parser("resume", help="resume after Stop everything")
    sub.add_parser("gui", help="open the raylib GUI against a running engine")
    sub.add_parser("status", help="print team / board / questions")
    p = sub.add_parser("say", help="chat to an agent from the terminal")
    p.add_argument("agent")
    p.add_argument("text", nargs="+")
    p = sub.add_parser("api", help="call the engine local API")
    p.add_argument("method")
    p.add_argument("params", nargs="?", default="{}")
    sub.add_parser("doctor", help="check backends are available")
    args = ap.parse_args()
    handlers = {"init": cmd_init, "tui": cmd_tui, "up": cmd_up, "engine": cmd_engine, "gui": cmd_gui,
                "status": cmd_status, "say": cmd_say, "doctor": cmd_doctor, "api": cmd_api, "start": cmd_start,
                "stop": cmd_stop, "resume": cmd_resume, "restart": cmd_restart, "projects": cmd_projects,
                "ps": cmd_projects}
    handlers.get(args.cmd or "tui", cmd_tui)(args)


if __name__ == "__main__":
    main()
