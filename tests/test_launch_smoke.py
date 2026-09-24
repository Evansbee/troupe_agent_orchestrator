"""REQ-ENG-059: the merge gate's pytest-selectable wrapper around scripts/launch_smoke.py.

QA's rule: a skip is never a pass. If the environment can't open a window (no display), this
skips with the reason printed instead of silently succeeding -- see scripts/launch_smoke.py's
"SKIP:" convention, which the merge gate (gates.py) also refuses to count as a pass.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "launch_smoke.py"


def _load_launch_smoke():
    """scripts/launch_smoke.py isn't a package (no __init__.py, not installed) -- load it as a
    module by path so #112's classify_probe_result/can_open_a_window can be unit tested directly,
    the same module test_merge_gate.py's fake-script approach can't reach (that only invokes it as
    a subprocess, matching how gates.py's protected run_launch_smoke does)."""
    spec = importlib.util.spec_from_file_location("launch_smoke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launch_smoke = _load_launch_smoke()


def _real_registry_launch_smoke_count() -> int:
    """QA blocker #4: never touch the human's real ~/.troupe/projects.json. This machine runs a
    live, shared team, so other unrelated processes append to the real registry constantly -- a
    raw file hash comparison is not reliable here. Counting entries this script itself would name
    "launch-smoke" is a check that survives that noise."""
    path = Path.home() / ".troupe" / "projects.json"
    try:
        rows = json.loads(path.read_text())
    except (OSError, ValueError):
        return 0
    return sum(1 for r in rows if r.get("name") == "launch-smoke")


@pytest.mark.launch_smoke
def test_launch_smoke():
    before = _real_registry_launch_smoke_count()
    try:
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=55)
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"launch smoke did not finish within 55s:\n{(e.stderr or '')[-4000:]}")
    assert _real_registry_launch_smoke_count() == before, "touched the real ~/.troupe/projects.json"
    if "SKIP:" in result.stdout:
        pytest.skip(next(line for line in result.stdout.splitlines() if line.startswith("SKIP:")))
    assert result.returncode == 0, result.stderr[-4000:]


@pytest.mark.parametrize("output", [
    "Traceback (most recent call last):\n  File \"<string>\", line 4, in <module>\nImportError: x",
    "Traceback (most recent call last):\nZeroDivisionError: division by zero",
])
def test_classify_probe_result_traceback_always_fails(output):
    """#112: a probe traceback (e.g. a broken/shadowed pyray import) must FAIL, never be read as
    "no display" -- QA's exact repro: `raise ImportError(...)` in src/pyray.py used to skip."""
    verdict, detail = launch_smoke.classify_probe_result(1, output)
    assert verdict == "fail"
    assert "Traceback" in detail


def test_classify_probe_result_timeout_via_can_open_a_window_fails_after_one_retry():
    """#112: a probe that exceeds PROBE_TIMEOUT (e.g. a loaded machine) must FAIL, not skip --
    but gets one retry first, since a one-off slow probe isn't necessarily "no display" either."""
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        raise subprocess.TimeoutExpired(cmd="probe", timeout=launch_smoke.PROBE_TIMEOUT)

    with patch.object(launch_smoke.subprocess, "run", side_effect=fake_run):
        verdict, detail = launch_smoke.can_open_a_window({})
    assert verdict == "fail"
    assert "timed out" in detail
    assert len(calls) == launch_smoke.PROBE_TIMEOUT_RETRIES + 1  # the retry actually happened


def test_can_open_a_window_succeeds_on_a_retry_after_one_timeout():
    """The retry isn't just counted -- a probe that times out once and then succeeds must be read
    as "ok", not "fail" (the whole point of retrying instead of failing on the first timeout)."""
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd="probe", timeout=launch_smoke.PROBE_TIMEOUT)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch.object(launch_smoke.subprocess, "run", side_effect=fake_run):
        verdict, detail = launch_smoke.can_open_a_window({})
    assert verdict == "ok"
    assert len(calls) == 2


@pytest.mark.parametrize("marker", launch_smoke.NO_DISPLAY_MARKERS)
def test_classify_probe_result_known_no_display_text_skips(marker):
    """#112 acceptance: a probe that exits 1 with the known no-display text -> SKIP."""
    verdict, detail = launch_smoke.classify_probe_result(1, f"WARNING: {marker} (some detail)\n")
    assert verdict == "skip"
    assert marker in detail


def test_classify_probe_result_unrecognized_failure_text_fails_not_skips():
    """#112: the core of the bug -- ANY non-zero exit used to be read as "no display". A failure
    that isn't a traceback and isn't a recognized no-display signal must still FAIL, not skip,
    since it's neither proven "no display" nor a Python-level crash we already catch above."""
    verdict, detail = launch_smoke.classify_probe_result(1, "some unrelated stderr noise\n")
    assert verdict == "fail"
    assert "unrecognized" in detail


def test_classify_probe_result_signal_kill_fails():
    """A negative returncode (Python's convention for "killed by signal N") is never "no display"
    -- e.g. a segfault while probing must FAIL, matching the same rule the real client's own exit
    already got (QA blocker #3's comment in scripts/launch_smoke.py)."""
    verdict, detail = launch_smoke.classify_probe_result(-11, "")
    assert verdict == "fail"
    assert "signal 11" in detail


def test_classify_probe_result_clean_exit_is_ok():
    verdict, detail = launch_smoke.classify_probe_result(0, "")
    assert verdict == "ok"
    assert detail == ""


def test_shadowed_pyray_import_error_makes_the_real_probe_fail(tmp_path, monkeypatch):
    """#112 acceptance criterion 3 (QA's exact repro), automated: shadow pyray with a module that
    raises ImportError, on the real can_open_a_window() (not just classify_probe_result), and
    confirm it fails rather than silently skipping. Writes to a scratch dir prepended ahead of the
    real src/ on sys.path for the probe subprocess only -- never touches the actual tree's src/."""
    shadow_src = tmp_path / "shadow_src"
    shadow_src.mkdir()
    (shadow_src / "pyray.py").write_text('raise ImportError("simulated shadowed pyray")\n')
    probe = launch_smoke.WINDOW_PROBE.replace(
        f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r})",
        f"sys.path.insert(0, {str(shadow_src)!r})\nsys.path.insert(0, {str(REPO_ROOT / 'src')!r})",
    )
    monkeypatch.setattr(launch_smoke, "WINDOW_PROBE", probe)
    verdict, detail = launch_smoke.can_open_a_window(dict(__import__("os").environ))
    assert verdict == "fail"
    assert "simulated shadowed pyray" in detail
