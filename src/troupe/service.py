"""Small on-disk status contract for clients when the socket is unavailable."""

import json
import os
import tempfile
import time
from pathlib import Path


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
