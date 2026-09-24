""""Run and everything runs, quit and everything quits" (REQ-TUI-001): the TUI starts its
project's engine as a child (reusing #24's service.py lock/identity machinery) unless one is
already running, in which case it either attaches without owning it (someone else is plausibly
still responsible for it) or re-adopts it (its previous owner is dead — #100 F3). service.py's
start/stop are synchronous and poll with time.sleep, so every call here goes through
asyncio.to_thread to avoid blocking the Textual event loop."""
from __future__ import annotations

import asyncio
import os

from .. import config as config_mod
from ..service import kill_service_now, owner_alive, service_status, set_owner, start_service, stop_service


async def ensure_engine(cfg: config_mod.Config) -> bool:
    """Start this project's engine if none is running; if one is already running, adopt it when
    its recorded owner is no longer alive (e.g. a previous TUI was `kill -9`'d — F3's orphan
    re-adopt), else attach without owning it. Returns True if we now own it (and so should stop it
    on quit/Ctrl-C/SIGHUP), False if we merely attached to someone else's live engine."""
    running = await asyncio.to_thread(lambda: service_status(cfg.root)["state"] == "running")
    adopt = running and not await asyncio.to_thread(owner_alive, cfg.root)
    await asyncio.to_thread(start_service, cfg)
    owns = adopt or not running
    if owns:
        await asyncio.to_thread(set_owner, cfg.state_dir, os.getpid())
    return owns


async def stop_owned_engine(cfg: config_mod.Config, timeout: float = 15) -> None:
    await asyncio.to_thread(stop_service, cfg, timeout)


async def kill_owned_engine_now(cfg: config_mod.Config) -> None:
    """REQ-TUI-001: a second Ctrl-C escalates straight to SIGKILL, no grace period."""
    await asyncio.to_thread(kill_service_now, cfg)


async def restart_engine(cfg: config_mod.Config) -> bool:
    """`r`: restart a dead engine. Only meaningful when it's actually offline."""
    status = await asyncio.to_thread(lambda: service_status(cfg.root))
    if status["state"] == "running":
        return False
    await asyncio.to_thread(start_service, cfg)
    await asyncio.to_thread(set_owner, cfg.state_dir, os.getpid())
    return True
