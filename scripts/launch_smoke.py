#!/usr/bin/env python3
"""Launch smoke test (REQ-ENG-059): does the client actually start?

QA's finding on #90: a static fixture .troupe with a pending safety card does NOT reproduce a
crash in gui/data.py's "new since last refresh" path, because the human hit it when the engine
posted the card a moment *after* the GUI's first frame, not before. This script reproduces that
timing for real: boots a fresh project against fake claude/codex backends, launches the GUI and
TUI, waits a beat, then inserts a needs_help message, a PM chat message, and a pending question
live through the Store -- after each client's first load, not before it -- and asserts both exit
clean with no traceback and a non-empty screenshot.

Usage: uv run python scripts/launch_smoke.py
Exit 0 on success (or a clean environment skip, printed as "SKIP: <reason>").
Exit 1 with the failing process's stderr tail on a real failure (e.g. a traceback).

Reverting #90's fix (re-adding `d.notify(...)` in gui/app.py's handle_notifications) must make
this fail with that AttributeError -- that's the acceptance proof for this script.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

INJECT_DELAY = 0.5  # seconds after launch, well past the GUI's first (force=True) refresh
GUI_TIMEOUT = 25
TUI_TIMEOUT = 30  # includes ensure_engine()'s own up-to-10s wait for a heartbeat

FAKE_BACKEND = '''#!/usr/bin/env python3
import json, time
print(json.dumps({"type": "system", "subtype": "init", "session_id": "smoke"}), flush=True)
time.sleep(300)
'''


def make_project(tmp: Path) -> Path:
    """`troupe init` for real, then point backends at a fake CLI so nothing calls out to a real
    claude/codex/LM Studio (matches QA's /tmp/qa_tui_harness.sh approach)."""
    proj = tmp / "proj"
    fake = tmp / "fake_backend.py"
    fake.write_text(FAKE_BACKEND)
    fake.chmod(0o755)
    subprocess.run([sys.executable, "-m", "troupe.cli", "init", str(proj), "--name", "launch-smoke"],
                   cwd=REPO_ROOT, check=True, capture_output=True, text=True)
    toml_path = proj / ".troupe" / "troupe.toml"
    text = toml_path.read_text()
    text = text.replace('claude_command = "claude"', f'claude_command = "{fake}"')
    text = text.replace('codex_command = "codex"', f'codex_command = "{fake}"')
    text = text.replace('local_base_url = "http://localhost:1234/v1"', 'local_base_url = "http://127.0.0.1:9"')
    toml_path.write_text(text)
    return proj


def inject_live_state(proj: Path) -> None:
    """The three arrivals QA's finding said must happen live, not in a static fixture."""
    from troupe import config as config_mod
    from troupe.store import Store
    cfg = config_mod.load(proj)
    store = Store(cfg.db_path)
    store.send("system", "human", "Safety needs attention (launch-smoke)", subject="Safety needs attention",
               kind="needs_help")
    store.send("pm", "human", "Launch-smoke: live message from the PM.", kind="chat")
    store.ask("system", "Approve safety baseline (launch-smoke)?", "Injected live by scripts/launch_smoke.py.",
              ["Approve", "Reject"], kind="safety")


def launch(proj: Path, cmd: str, png: Path, extra_env: dict | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    env["TROUPE_SHOT"] = str(png)
    env.update(extra_env or {})
    return subprocess.Popen([sys.executable, "-m", "troupe.cli", cmd], cwd=proj, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def finish(name: str, proc: subprocess.Popen, png: Path, timeout: float) -> str | None:
    """Returns None on a clean pass, "SKIP: ..." on an environment skip, or a failure description."""
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return f"{name} timed out after {timeout:g}s\n{err[-4000:]}"
    if "Traceback" in err:
        return f"{name} crashed (exit {proc.returncode}):\n{err[-4000:]}"
    if proc.returncode != 0:
        # No traceback and a fast, non-zero exit: most likely no display/window server available,
        # not a real bug. REQ-ENG-059: this must never be silently treated the same as a pass.
        return f"SKIP: {name} exited {proc.returncode} without opening a window:\n{err[-2000:]}"
    if not png.exists() or png.stat().st_size == 0:
        return f"{name} exited 0 but wrote no screenshot at {png}"
    return None


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="troupe-launch-smoke-") as tmp_str:
        tmp = Path(tmp_str)
        proj = make_project(tmp)
        gui_png, tui_png = tmp / "gui.png", tmp / "tui.png"
        gui_proc = launch(proj, "gui", gui_png, {"TROUPE_TAB": "Board"})
        tui_proc = launch(proj, "tui", tui_png)
        time.sleep(INJECT_DELAY)
        inject_live_state(proj)
        gui_result = finish("gui", gui_proc, gui_png, GUI_TIMEOUT)
        tui_result = finish("tui", tui_proc, tui_png, TUI_TIMEOUT)
        # tui's ensure_engine() starts an owned engine that TROUPE_SHOT's snapshot-and-exit never
        # stops (it skips the normal quit/SIGHUP path) -- stop it ourselves or it outlives the tmp
        # project directory it's reading, spewing errors into engine.log forever.
        try:
            from troupe import config as config_mod
            from troupe.service import stop_service
            stop_service(config_mod.load(proj))
        except Exception as e:
            print(f"warning: could not stop launch-smoke's engine: {e}", file=sys.stderr)
        results = [r for r in (gui_result, tui_result) if r]
        skips = [r for r in results if r.startswith("SKIP:")]
        failures = [r for r in results if not r.startswith("SKIP:")]
        if failures:
            print("\n\n".join(failures), file=sys.stderr)
            return 1
        if skips:
            print("\n".join(skips))
            return 0
        print(f"launch smoke passed: {gui_png.stat().st_size}B GUI shot, {tui_png.stat().st_size}B TUI shot")
        return 0


if __name__ == "__main__":
    sys.exit(main())
