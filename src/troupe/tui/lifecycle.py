""""Run and everything runs, quit and everything quits" (REQ-TUI-001): the TUI starts its
project's engine as a child (reusing #24's service.py lock/identity machinery) unless one is
already running, in which case it attaches without owning it. service.py's start/stop are
synchronous and poll with time.sleep, so every call here goes through asyncio.to_thread to avoid
blocking the Textual event loop."""
from __future__ import annotations

import asyncio

from .. import config as config_mod
from ..service import service_status, start_service, stop_service


async def ensure_engine(cfg: config_mod.Config) -> bool:
    """Start this project's engine if none is running, else leave the existing one alone.
    Returns True if we own it (and so should stop it on quit/SIGHUP), False if we attached."""
    already_running = await asyncio.to_thread(lambda: service_status(cfg.root)["state"] == "running")
    await asyncio.to_thread(start_service, cfg)
    return not already_running


async def stop_owned_engine(cfg: config_mod.Config) -> None:
    await asyncio.to_thread(stop_service, cfg)


async def restart_engine(cfg: config_mod.Config) -> bool:
    """`r`: restart a dead engine. Only meaningful when it's actually offline."""
    status = await asyncio.to_thread(lambda: service_status(cfg.root))
    if status["state"] == "running":
        return False
    await asyncio.to_thread(start_service, cfg)
    return True
