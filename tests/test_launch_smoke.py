"""REQ-ENG-059: the merge gate's pytest-selectable wrapper around scripts/launch_smoke.py.

QA's rule: a skip is never a pass. If the environment can't open a window (no display), this
skips with the reason printed instead of silently succeeding -- see scripts/launch_smoke.py's
"SKIP:" convention, which the merge gate (gates.py) also refuses to count as a pass.
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "launch_smoke.py"


@pytest.mark.launch_smoke
def test_launch_smoke():
    try:
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=55)
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"launch smoke did not finish within 55s:\n{(e.stderr or '')[-4000:]}")
    if "SKIP:" in result.stdout:
        pytest.skip(next(line for line in result.stdout.splitlines() if line.startswith("SKIP:")))
    assert result.returncode == 0, result.stderr[-4000:]
