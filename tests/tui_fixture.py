"""A minimal fixture API server for TUI tests (REQ-TUI-031): a real asyncio Unix-socket server
speaking the same NDJSON request/response + pushed-event protocol as api.py, with canned data and
no engine required."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path


class FixtureServer:
    def __init__(self, root: Path, *, agents=None, tasks=None, usage=None, engine=None, milestones=None,
                 messages=None, memories=None, errors=None, questions=None):
        self.root = root
        self.agents = agents if agents is not None else []
        self.tasks = tasks if tasks is not None else []
        self.usage = usage if usage is not None else {"providers": []}
        self.engine = engine if engine is not None else {"state": "live", "running_runs": 0}
        self.milestones = milestones if milestones is not None else []
        self.messages = messages if messages is not None else []
        self.memories = memories if memories is not None else []
        self.errors: dict[str, tuple[str, str]] = errors if errors is not None else {}
        self.questions = questions if questions is not None else []
        self.seq = 0
        self._server: asyncio.base_events.Server | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.commands: list[tuple[str, dict]] = []

    async def start(self) -> Path:
        from troupe.api import socket_path
        path = socket_path(self.root, create=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(self._handle, path=str(path))
        return path

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            if self._writer is not None:
                self._writer.close()  # in case the client didn't close its end first
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def push_event(self, name: str, data: dict | None = None) -> None:
        self.seq += 1
        obj = dict(event=name, seq=self.seq, data=data or {})
        if self._writer is not None:
            self._writer.write((json.dumps(obj) + "\n").encode())
            await self._writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                req = json.loads(line)
                method, params = req["method"], req.get("params", {})
                if method in self.errors:
                    code, message = self.errors[method]
                    writer.write((json.dumps(dict(id=req["id"], ok=False,
                                                  error=dict(code=code, message=message))) + "\n").encode())
                    await writer.drain()
                    continue
                if method in ("chat", "answer_question", "stop_run", "stop_now", "resume", "stop_team",
                              "wake", "dismiss_question", "mark_chat_read"):
                    self.commands.append((method, params))
                    result = {}
                else:
                    result = self._dispatch(method, params)
                writer.write((json.dumps(dict(id=req["id"], ok=True, result=result)) + "\n").encode())
                await writer.drain()
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            if self._writer is writer:
                self._writer = None

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "hello":
            return dict(api_version=0, project="test-project", handle_suffix="@test",
                        root=str(self.root), troupe_version="0.1.0", epoch=1, seq=self.seq,
                        server_time=0.0, engine=self.engine)
        if method == "agents":
            return dict(items=self.agents)
        if method == "tasks":
            return dict(items=self.tasks)
        if method == "usage":
            return self.usage
        if method == "engine":
            return self.engine
        if method == "milestones":
            return dict(items=self.milestones)
        if method == "messages":
            items = self.messages
            if params.get("kind"):
                items = [m for m in items if m["kind"] == params["kind"]]
            return dict(items=items)
        if method == "memories":
            items = self.memories
            if params.get("kind"):
                items = [m for m in items if m["kind"] == params["kind"]]
            return dict(items=items)
        if method == "questions":
            status = params.get("status", "open")
            items = self.questions if status in ("open", "all") else []
            return dict(items=items)
        if method == "subscribe":
            return dict(epoch=1, seq=self.seq)
        return {}
