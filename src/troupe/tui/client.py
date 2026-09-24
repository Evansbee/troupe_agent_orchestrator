"""Async API client for the TUI (REQ-TUI-002): NDJSON over the private Unix socket, the same
wire protocol as api_client.Client but non-blocking, since the TUI runs on Textual's asyncio loop.
Every socket operation has a hard timeout (the #49 lesson: a hung call must never hang the UI)."""
from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator

from .. import __version__
from ..api import MAX_LINE, APIError, socket_path

DEFAULT_TIMEOUT = 5.0
RECONNECT_DELAY = 1.0


class TuiClient:
    def __init__(self, root: Path, *, notifications: bool = False):
        self.root = root
        self.notifications = notifications
        self.hello: dict | None = None
        self.connected = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._counter = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._push: deque[dict] = deque()
        self._push_ready = asyncio.Event()
        self._pump_task: asyncio.Task | None = None

    async def connect(self, timeout: float = DEFAULT_TIMEOUT) -> dict:
        """Open the socket and say hello. Raises on failure — callers decide how to show offline.

        #108: asyncio's own default StreamReader line limit is 64 KiB; a `tasks`/`messages`/etc.
        response for a realistically sized project is comfortably over that, and readline() raises
        LimitOverrunError (a ValueError) once a line exceeds it, which _pump below turns into
        "engine offline" for every pending call. Import the server's own MAX_LINE (api.py) instead
        of picking a new number, so the two ends can't drift apart again."""
        path = socket_path(self.root)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path), limit=MAX_LINE), timeout)
        self.connected = True
        self._pump_task = asyncio.create_task(self._pump())
        self.hello = await self.call(
            "hello", timeout=timeout, api_version=0,
            client=f"troupe-tui/{__version__}", notifications=self.notifications,
        )
        return self.hello

    async def close(self) -> None:
        self.connected = False
        if self._pump_task is not None:
            self._pump_task.cancel()
            self._pump_task = None
        if self._writer is not None:
            writer, self._writer = self._writer, None
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("closed"))
        self._pending.clear()

    async def _pump(self) -> None:
        """Reads every line, demultiplexing call responses from pushed events (same socket)."""
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                obj = json.loads(line)
                if "event" in obj:
                    self._push.append(obj)
                    self._push_ready.set()
                    continue
                fut = self._pending.pop(obj.get("id"), None)
                if fut is not None and not fut.done():
                    fut.set_result(obj)
        except (asyncio.CancelledError, ConnectionError, OSError, ValueError):
            pass
        finally:
            self.connected = False
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("engine offline"))
            self._push_ready.set()  # wake any events() waiter so it notices the disconnect

    async def call(self, method: str, timeout: float = DEFAULT_TIMEOUT, **params: Any) -> dict:
        if not self.connected or self._writer is None:
            raise ConnectionError("not connected")
        self._counter += 1
        cid = self._counter
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        try:
            self._writer.write((json.dumps(dict(id=cid, method=method, params=params)) + "\n").encode())
            await asyncio.wait_for(self._writer.drain(), timeout)
            response = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(cid, None)
        if not response["ok"]:
            e = response["error"]
            raise APIError(e["code"], e["message"], e.get("data"))
        return response["result"]

    async def events(self) -> AsyncIterator[dict]:
        """The subscribe stream, reconnecting (with resync from the last seen seq) on any drop.
        Yields pushed {event, seq, data} dicts forever — callers just `async for`."""
        since = 0
        while True:
            try:
                if not self.connected:
                    await self.connect()
                    since = self.hello["seq"]
                await self.call("subscribe", since_seq=since)
                while self.connected:
                    while self._push:
                        ev = self._push.popleft()
                        if ev.get("event") == "resync_required":
                            raise ConnectionError("resync_required")
                        since = ev.get("seq", since)
                        yield ev
                    self._push_ready.clear()
                    await self._push_ready.wait()
            except (ConnectionError, OSError, APIError, asyncio.TimeoutError):
                await self.close()
                await asyncio.sleep(RECONNECT_DELAY)
