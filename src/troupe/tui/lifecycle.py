""""Run and everything runs, quit and everything quits" (REQ-TUI-001): the TUI starts its
project's engine as a child (reusing #24's service.py lock/identity machinery) unless one is
already running, in which case it either attaches without owning it (a `troupe up`/`start`/
`engine` -started engine, or another live TUI's) or re-adopts it (its previous TUI owner has died
-- #100 F3). service.py's start/stop are synchronous and poll with time.sleep, so every call here
goes through asyncio.to_thread to avoid blocking the Textual event loop."""
from __future__ import annotations

import asyncio
import os

from .. import config as config_mod
from ..service import kill_service_now, service_status, set_owner, start_service, stop_service, tui_owner_is_dead


async def ensure_engine(cfg: config_mod.Config) -> bool:
    """Start this project's engine if none is running (recording ourselves as its "tui" owner); if
    one is already running, adopt it only when its recorded owner is itself a dead TUI (F3's orphan
    re-adopt) -- QA's #100 regression: an engine `troupe up`/`start`/`engine` started, or one with
    no owner recorded at all (pre-#100), is NEVER adopted just because nothing claims it; that's the
    normal, intended way those keep running independent of any TUI. Returns True if we now own it
    (and so should stop it on quit/Ctrl-C/SIGHUP), False if we merely attached."""
    running = await asyncio.to_thread(lambda: service_status(cfg.root)["state"] == "running")
    adopt = running and await asyncio.to_thread(tui_owner_is_dead, cfg.root)
    await asyncio.to_thread(start_service, cfg, owner="tui")
    owns = adopt or not running
    if adopt:
        # start_service only records ownership for a spawn it performs itself (the `not running`
        # branch above); adopting a still-running orphan needs its own explicit claim.
        await asyncio.to_thread(set_owner, cfg.state_dir, os.getpid(), "tui")
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
    await asyncio.to_thread(start_service, cfg, owner="tui")  # always a fresh spawn here (offline)
    return True
