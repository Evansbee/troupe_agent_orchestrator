#!/usr/bin/env python3
"""Launch smoke test (REQ-ENG-059): does the client actually start?

QA's finding on #90: a static fixture .troupe with a pending safety card does NOT reproduce a
crash in gui/data.py's "new since last refresh" path, because the human hit it when the engine
posted the card a moment *after* the GUI's first frame, not before. This script reproduces that
timing for real: boots a fresh project against fake claude/codex backends, launches the GUI and
TUI, then keeps injecting a needs_help message, a PM chat message and a pending question live
through the Store -- until each client exits -- so at least one arrival always lands after that
client's first load, whenever that happens to be, instead of racing one fixed delay against it.

Usage: uv run python scripts/launch_smoke.py   (run from a checkout: `uv run` picks up ITS OWN
    venv, which is what the merge gate must invoke too -- see gates.py's run_launch_smoke. Running
    this via the wrong Python (e.g. an installed troupe's own interpreter) imports whatever's
    installed there, not this tree's code, and defeats the whole point of a merge-gate check.)
Exit 0 on success (or a clean, positively-detected "no display" skip, printed as "SKIP: <reason>").
Exit 1 with the failing process's stderr tail on any other non-zero exit or a traceback.

Reverting #90's fix (re-adding `d.notify(...)` in gui/app.py's handle_notifications) must make
this fail with that AttributeError -- that's the acceptance proof for this script.

Never touches the real human's ~/.troupe: HOME (and XDG dirs) point into this run's own temp
directory for `troupe init` and both client subprocesses.
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

INJECT_INTERVAL = 0.2  # seconds between re-injections while either client is still running
GUI_SHOT_FRAME = 200  # ~3s+ of real runtime at a typical 60fps -- long enough that some
# injection interval reliably lands after the GUI's first (force=True) refresh, whenever that is
GUI_TIMEOUT = 30
TUI_TIMEOUT = 30  # includes ensure_engine()'s own up-to-10s wait for a heartbeat
PROBE_TIMEOUT = 10

FAKE_BACKEND = '''#!/usr/bin/env python3
import json, time
print(json.dumps({"type": "system", "subtype": "init", "session_id": "smoke"}), flush=True)
time.sleep(300)
'''

# QA blocker #3: decide "no display" up front and positively, before the real GUI ever runs, by
# actually trying to open a throwaway window -- not by guessing from the real run's exit code
# afterward. Once past this point, any non-zero exit from the real client (including a negative
# one, i.e. killed by a signal such as a segfault) is an unambiguous FAILURE, never a skip.
#
# LOG_WARNING, not LOG_NONE (#112): raylib's own InitWindow() gracefully reports "couldn't open a
# window" back to Python via is_window_ready() == False, with no exception and no crash -- but it
# only says *why* (the "Failed to initialize GLFW/Window" text this script matches on below) via
# its own TraceLog warning. LOG_NONE suppressed that along with everything else, leaving nothing
# to positively identify -- which is exactly how any failure, not just "no display", used to look
# identical and get silently skipped.
WINDOW_PROBE = f"""
import sys
sys.path.insert(0, {str(REPO_ROOT / "src")!r})
import pyray as rl
rl.set_trace_log_level(rl.TraceLogLevel.LOG_WARNING)
rl.init_window(2, 2, "launch-smoke-probe")
ok = rl.is_window_ready()
if ok:
    rl.close_window()
sys.exit(0 if ok else 1)
"""

# The only text this script trusts as a *positive* "there is genuinely no display here" signal
# (#112) -- raylib's own rcore.c TraceLog wording when GLFW can't initialize or can't create a
# window, which is the graceful, no-exception, no-crash path InitWindow takes when there's no
# display to open a window on. Extend this list if a real environment's exact wording turns up
# something new; anything NOT on this list is a FAILURE, never a silent skip -- a probe traceback
# (e.g. a broken pyray import), a signal, a timeout, or any other unrecognized failure must never
# be read as "no display", since that's exactly the hole that let the #90 crash class through.
NO_DISPLAY_MARKERS = (
    "Failed to initialize GLFW",
    "Failed to initialize Window",
    "Cocoa: Failed to find service port for display",  # macOS: no WindowServer session (headless/CI)
    "X11: Failed to open display",
    "X11: The DISPLAY environment variable is missing",
    "Wayland: Failed to connect to display",
)

PROBE_TIMEOUT_RETRIES = 1  # a timeout on a loaded machine gets one retry, then it's a FAILURE


def isolated_env(tmp: Path) -> dict:
    """QA blocker #4: `troupe init` and both clients call service.register_project(), which reads
    Path.home() -- pointing HOME here keeps every run out of the human's real ~/.troupe."""
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
              XDG_DATA_HOME=str(home / ".local/share"), XDG_CACHE_HOME=str(home / ".cache"))
    return env


def classify_probe_result(returncode: int, output: str) -> tuple[str, str]:
    """Pure classification of an already-completed probe run: "ok" (a real window opened), "skip"
    (a positively recognized no-display signal -- the *only* case allowed to skip the real
    GUI/TUI launch), or "fail" (anything else). Split out from can_open_a_window so the decision
    logic is directly testable without spawning a real subprocess for every case (#112)."""
    if returncode == 0:
        return "ok", ""
    if "Traceback (most recent call last)" in output:
        return "fail", f"window probe raised an exception:\n{output.strip()[-2000:]}"
    if returncode < 0:
        return "fail", f"window probe was killed by signal {-returncode}:\n{output.strip()[-2000:]}"
    if any(marker in output for marker in NO_DISPLAY_MARKERS):
        return "skip", (output.strip() or f"window probe exited {returncode}")[-500:]
    return "fail", (f"window probe exited {returncode} with unrecognized output "
                    f"(not a known no-display signal):\n{output.strip()[-2000:]}")


def _run_probe_once(env: dict) -> subprocess.CompletedProcess | None:
    """None means the probe timed out."""
    try:
        return subprocess.run([sys.executable, "-c", WINDOW_PROBE], cwd=REPO_ROOT, env=env,
                              capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None


def can_open_a_window(env: dict) -> tuple[str, str]:
    """Runs the real window probe (with PROBE_TIMEOUT_RETRIES retries on a timeout -- a loaded
    machine can cause a one-off slow probe that isn't actually "no display"), then classifies the
    result. Returns (verdict, detail): verdict is "ok", "skip", or "fail" -- see
    classify_probe_result."""
    for attempt in range(PROBE_TIMEOUT_RETRIES + 1):
        p = _run_probe_once(env)
        if p is not None:
            return classify_probe_result(p.returncode, (p.stderr or "") + (p.stdout or ""))
    attempts = PROBE_TIMEOUT_RETRIES + 1
    return "fail", f"window probe timed out {attempts} time{'s' if attempts != 1 else ''} in a row ({PROBE_TIMEOUT}s each)"


def make_project(tmp: Path, env: dict) -> Path:
    """`troupe init` for real, then point backends at a fake CLI so nothing calls out to a real
    claude/codex/LM Studio (matches QA's /tmp/qa_tui_harness.sh approach)."""
    proj = tmp / "proj"
    fake = tmp / "fake_backend.py"
    fake.write_text(FAKE_BACKEND)
    fake.chmod(0o755)
    subprocess.run([sys.executable, "-m", "troupe.cli", "init", str(proj), "--name", "launch-smoke"],
                   cwd=REPO_ROOT, env=env, check=True, capture_output=True, text=True)
    toml_path = proj / ".troupe" / "troupe.toml"
    text = toml_path.read_text()
    text = text.replace('claude_command = "claude"', f'claude_command = "{fake}"')
    text = text.replace('codex_command = "codex"', f'codex_command = "{fake}"')
    text = text.replace('local_base_url = "http://localhost:1234/v1"', 'local_base_url = "http://127.0.0.1:9"')
    toml_path.write_text(text)
    return proj


def inject_live_state(proj: Path) -> None:
    """The three arrivals QA's finding said must happen live, not in a static fixture. Called
    repeatedly (main() re-injects on an interval) so timing can never race a fixed delay."""
    from troupe import config as config_mod
    from troupe.store import Store
    cfg = config_mod.load(proj)
    store = Store(cfg.db_path)
    pm = next(a.id for a in cfg.agents if a.role == "pm")
    store.send("system", "human", "Safety needs attention (launch-smoke)", subject="Safety needs attention",
               kind="needs_help")
    store.send(pm, "human", "Launch-smoke: live message from the PM.", kind="chat")
    store.ask("system", "Approve safety baseline (launch-smoke)?", "Injected live by scripts/launch_smoke.py.",
              ["Approve", "Reject"], kind="safety")


def launch(proj: Path, cmd: str, png: Path, env: dict, extra_env: dict | None = None) -> subprocess.Popen:
    run_env = dict(env, TROUPE_SHOT=str(png))
    run_env.update(extra_env or {})
    return subprocess.Popen([sys.executable, "-m", "troupe.cli", cmd], cwd=proj, env=run_env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def finish(name: str, proc: subprocess.Popen, png: Path, timeout: float) -> str | None:
    """Returns None on a clean pass, or a failure description. Skip is decided up front by
    can_open_a_window(), never inferred here from the real client's own exit code -- QA blocker #3:
    a signal (e.g. a segfault) or any other non-zero exit past that point is always a failure."""
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return f"{name} timed out after {timeout:g}s\n{err[-4000:]}"
    if proc.returncode != 0:
        return f"{name} failed (exit {proc.returncode}):\n{err[-4000:]}"
    if not png.exists() or png.stat().st_size == 0:
        return f"{name} exited 0 but wrote no screenshot at {png}"
    return None


def stop_any_engine(proj: Path) -> None:
    # tui's ensure_engine() starts an owned engine that TROUPE_SHOT's snapshot-and-exit never
    # stops (it skips the normal quit/SIGHUP path) -- stop it here or it outlives the tmp project
    # directory it's reading, spewing errors into engine.log forever.
    try:
        from troupe import config as config_mod
        from troupe.service import stop_service
        stop_service(config_mod.load(proj))
    except Exception as e:
        print(f"warning: could not stop launch-smoke's engine: {e}", file=sys.stderr)


def main() -> int:
    # dir="/tmp", not the default (macOS's per-user /var/folders/<hash>/T/...): a Unix domain
    # socket path has a ~104-byte kernel limit, and the default tempdir's random per-user hash
    # component pushes proj/.troupe/api.sock over that intermittently -- a real, previously-latent
    # flake, not the #90 timing race this script exists to catch.
    with tempfile.TemporaryDirectory(prefix="troupe-launch-smoke-", dir="/tmp") as tmp_str:
        tmp = Path(tmp_str)
        env = isolated_env(tmp)
        verdict, detail = can_open_a_window(env)
        if verdict == "fail":
            # #112: a probe traceback, a signal, or a timeout (even after a retry) is never "no
            # display" -- it's a failure of the gate itself (a broken import, a broken environment,
            # or a real regression like #90's crash class), and the real GUI/TUI would only hit
            # the same problem, so this fails fast instead of spending the full run finding that out.
            print(detail, file=sys.stderr)
            return 1
        gui_ok = verdict == "ok"
        proj: Path | None = None
        gui_proc = tui_proc = None
        try:
            proj = make_project(tmp, env)
            gui_png, tui_png = tmp / "gui.png", tmp / "tui.png"
            if gui_ok:
                gui_proc = launch(proj, "gui", gui_png, env,
                                  {"TROUPE_TAB": "Board", "TROUPE_SHOT_FRAME": str(GUI_SHOT_FRAME)})
            tui_proc = launch(proj, "tui", tui_png, env)
            while (gui_proc is not None and gui_proc.poll() is None) or tui_proc.poll() is None:
                inject_live_state(proj)
                time.sleep(INJECT_INTERVAL)
            results = []
            if gui_proc is not None:
                results.append(finish("gui", gui_proc, gui_png, GUI_TIMEOUT))
            results.append(finish("tui", tui_proc, tui_png, TUI_TIMEOUT))
        finally:
            for proc in (gui_proc, tui_proc):
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait()
            if proj is not None:
                stop_any_engine(proj)
        failures = [r for r in results if r]
        if failures:
            print("\n\n".join(failures), file=sys.stderr)
            return 1
        if not gui_ok:
            print(f"SKIP: gui: {detail}")
            return 0
        print(f"launch smoke passed: {gui_png.stat().st_size}B GUI shot, {tui_png.stat().st_size}B TUI shot")
        return 0


if __name__ == "__main__":
    sys.exit(main())
