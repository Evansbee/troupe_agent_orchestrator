"""Local human API: NDJSON over a private Unix socket, independent of the scheduler."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import claude_window_cap
from .roles import ROLES
from .store import ALL_STATUSES, HandleBook, Store

MAX_LINE = 4 * 1024 * 1024
MAX_TEXT = 1024 * 1024
PUSH_LIMIT = 8 * 1024 * 1024
OUTPUT_LIMIT = 32 * 1024 * 1024
STAGED = {
    "room_message": ("REQ-COM-024", 4),
    "reload": ("REQ-ENG-009", 28),
    "comment_decision": ("REQ-COM-035", 27),
    "update_config": ("REQ-ENG-019", 5),
}
PARAMS = {
    "ping": "",
    "hello": "api_version client notifications",
    "snapshot": "messages_limit",
    "agents": "",
    "tasks": "status milestone_id",
    "task": "id",
    "milestones": "",
    "messages": "before_id limit agent chat_with kind room task_id",
    "questions": "status limit",
    "memories": "kind status major limit",
    "decision_comments": "decision_id",
    "runs": "agent before_id limit",
    "run_lines": "run_id after_seq limit",
    "activity": "before_id limit significant_only",
    "usage": "",
    "engine": "",
    "seen": "",
    "config": "",
    "chat": "agent text idempotency_key",
    "milestone": "action id name goal order status",
    "room_message": "text idempotency_key",
    "mark_chat_read": "agent",
    "answer_question": "id text decision",
    "dismiss_question": "id",
    "wake": "agent",
    "stop_run": "agent",
    "set_agent_enabled": "agent enabled",
    "new_session": "agent",
    "pause": "",
    "resume": "",
    "stop_now": "",
    "stop_team": "",
    "reload": "",
    "create_task": "title description acceptance territory role priority depends_on milestone_id idempotency_key",
    "update_task": "id fields",
    "add_task_note": "id text idempotency_key",
    "comment_decision": "decision_id body idempotency_key",
    "update_config": "file patch",
    "update_memory": "id pinned major title content rationale",
    "delete_memory": "id",
    "mark_seen": "key ts",
    "subscribe": "topics since_seq",
    "unsubscribe": "",
}
COMMANDS = set(list(PARAMS)[list(PARAMS).index("chat") :]) - {
    "subscribe",
    "unsubscribe",
}


class APIError(Exception):
    def __init__(self, code: str, message: str, data=None):
        super().__init__(message)
        self.code, self.data = code, data

    def object(self):
        return dict(
            code=self.code,
            message=str(self),
            **({"data": self.data} if self.data is not None else {}),
        )


def unavailable(req, task):
    raise APIError(
        "unavailable",
        f"{req} is not available yet (task #{task})",
        {"req": req, "task": task},
    )


# ── human-only enforcement (#57, Principle 0) ───────────────────────────────
# QA found that the 0600 socket permission doesn't distinguish the human from an agent's Bash tool:
# both run as the same OS user. This identifies the connecting process and refuses commands that
# only the human may issue (resume, answering/dismissing questions) when the caller looks like an
# agent. It's a best-effort layer against the easy, accidental path — a determined agent can still
# detach a process and clear TROUPE_AGENT first. Real enforcement is #43's sandbox job.
HUMAN_ONLY_METHODS = {"resume", "answer_question", "dismiss_question"}


def peer_pid(writer: asyncio.StreamWriter) -> int | None:
    """The pid of the process on the other end of this Unix socket connection, or None if it can't
    be determined (unsupported platform, or the peer already disconnected)."""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    try:
        if sys.platform == "darwin":
            return sock.getsockopt(0, 2)  # SOL_LOCAL, LOCAL_PEERPID (not exported by socket module)
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, _uid, _gid = struct.unpack("3i", raw)
        return pid
    except OSError:
        return None


def _pid_env_has_agent(pid: int) -> bool:
    """Whether `pid`'s own environment carries TROUPE_AGENT — set directly in the env every agent
    backend launches its CLI with (runners.child_env), so a shell command it runs inherits it too.
    Same-user processes' environments are readable via `ps`; never logs what it reads."""
    try:
        out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=1).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    return re.search(r"(?:^|\s)TROUPE_AGENT=", out) is not None


def _pid_ancestors(pid: int, limit: int = 32) -> set[int]:
    chain = {pid}
    current = pid
    for _ in range(limit):
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(current)],
                                 capture_output=True, text=True, timeout=1).stdout.strip()
            ppid = int(out)
        except (subprocess.SubprocessError, ValueError, OSError):
            break
        if ppid <= 1 or ppid in chain:
            break
        chain.add(ppid)
        current = ppid
    return chain


def caller_is_agent(writer: asyncio.StreamWriter, engine) -> bool:
    """True only when the connecting process looks like an agent (or one of its subprocesses), by
    either signal QA verified: its own env carries TROUPE_AGENT, or it descends from a currently
    running agent run's process. Undetermined (no pid, no live engine) fails open — this is defense
    in depth, not the sandbox boundary."""
    pid = peer_pid(writer)
    if pid is None:
        return False
    if _pid_env_has_agent(pid):
        return True
    if engine is not None:
        runner_pids = {r.proc.pid for r, _, _ in engine.running.values() if r.proc}
        if runner_pids and runner_pids & _pid_ancestors(pid):
            return True
    return False


def bounded(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if isinstance(item, str) and len(item.encode()) > MAX_TEXT:
                result[key] = item.encode()[:MAX_TEXT].decode("utf-8", "ignore")
                result["truncated"] = True
            else:
                result[key] = bounded(item)
        return result
    if isinstance(value, list):
        return [bounded(x) for x in value]
    return value


def redact(value):
    if isinstance(value, dict):
        return {
            k: "***"
            if re.search("key|token|secret|password|environment|^env$", k, re.I)
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(x) for x in value]
    return value


def socket_path(root: Path, *, create=False) -> Path:
    pointer = root / ".troupe/api.sock.path"
    if not create and pointer.exists():
        return Path(pointer.read_text().strip())
    path = root / ".troupe/api.sock"
    if len(os.fsencode(path)) > 100:
        path = (
            Path.home()
            / ".troupe/sock"
            / (hashlib.sha1(os.fsencode(root)).hexdigest()[:16] + ".sock")
        )
        if create:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            pointer.write_text(str(path))
    elif create:
        pointer.unlink(missing_ok=True)
    return path


def integer(p, key, default=None, minimum=0, maximum=None):
    value = p.get(key, default)
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise APIError(
            "bad_request",
            f"{key} must be an integer from {minimum}"
            + (f" to {maximum}" if maximum else ""),
        )
    return value


def string(p, key, default=None):
    value = p.get(key, default)
    if not isinstance(value, str):
        raise APIError("bad_request", f"{key} must be a string")
    return value


def boolean(p, key, default=None):
    value = p.get(key, default)
    if type(value) is not bool:
        raise APIError("bad_request", f"{key} must be a boolean")
    return value


class Data:
    def __init__(self, server):
        self.server = server
        self.s = Store(server.cfg.db_path)
        self.s.x(
            "CREATE TABLE IF NOT EXISTS api_idempotency(method TEXT, key TEXT, ts REAL, result TEXT, PRIMARY KEY(method,key))"
        )

    @property
    def cfg(self):
        return self.server.engine.cfg if self.server.engine else self.server.cfg

    def require(self, table, ident):
        row = self.s.one(f"SELECT * FROM {table} WHERE id=?", ident)
        if row is None:
            raise APIError("not_found", f"{table}: {ident} not found")
        return row

    def agent_id(self, address):
        if not isinstance(address, str):
            raise APIError("bad_request", "agent must be a string")
        try:
            ids = HandleBook(self.cfg.project, self.cfg.agents).resolve(address)
        except ValueError as e:
            raise APIError("not_found", str(e)) from e
        if len(ids) != 1 or not self.s.agent(ids[0]):
            raise APIError("not_found", f"agent {address} not found or ambiguous")
        return ids[0]

    def agents(self):
        s = self.s
        rows = {a["id"]: a for a in s.agents()}
        unread = {
            r["sender"]: r["n"]
            for r in s.q(
                "SELECT sender,count(*) n FROM messages WHERE recipient='human' AND kind='chat' AND read_at IS NULL GROUP BY sender"
            )
        }
        owed = {
            r["assignee"]
            for r in s.q(
                "SELECT DISTINCT assignee FROM tasks WHERE status IN ('ready','in_progress')"
            )
        }
        names = HandleBook(self.cfg.project, self.cfg.agents)
        waits = s.wait_states()
        out = []
        for cfg in self.cfg.agents:
            a = rows.get(cfg.id)
            if not a:
                continue
            providers = [p.provider for p in cfg.providers] or [cfg.backend]
            out.append(
                {
                    k: a[k]
                    for k in (
                        "id",
                        "name",
                        "role",
                        "state",
                        "status",
                        "current_run",
                        "runs",
                        "tokens",
                        "cost",
                        "last_run_at",
                    )
                }
                | dict(
                    handle=names.name(a["id"]),
                    enabled=bool(a["enabled"]),
                    parked=a["state"] == "idle"
                    and bool(a["enabled"])
                    and a["id"] in owed
                    and time.time() - a["last_run_at"] > 180,
                    activity=a["activity"][:160],
                    providers=providers,
                    provider=a["backend"],
                    fallback=a["backend"] != providers[0],
                    model=a["model"],
                    level=cfg.level or None,
                    chat_unread=unread.get(a["id"], 0),
                    mail_queued=waits.get(a["id"], {}).get("mail_queued", 0),
                    mail_reading=waits.get(a["id"], {}).get("mail_reading", 0),
                    waiting_on=waits.get(a["id"], {}).get("waiting_on"),
                )
            )
        return out

    def task(self, row, detail=False, counts=None, flags=None):
        t = dict(row)
        if isinstance(t["depends_on"], str):
            t["depends_on"] = json.loads(t["depends_on"])
        t.setdefault("milestone_id", None)
        t["notes_count"] = (
            counts.get(t["id"], 0)
            if counts is not None
            else self.s.scalar(
                "SELECT count(*) FROM task_notes WHERE task_id=?", t["id"]
            )
        )
        failed = (
            flags.get(f"check_failed.{t['id']}", False)
            if flags is not None
            else self.s.kv_get(f"check_failed.{t['id']}", False)
        )
        t["flags"] = dict(
            checks_failed=bool(failed),
            arch_review=False,
            human_request=t["created_by"] == "human",
            territory_conflict=[],
        )
        if detail:
            t["notes"] = [
                dict(id=n["id"], ts=n["ts"], author=n["agent"], text=n["text"])
                for n in self.s.task_notes(t["id"])
            ]
            t["reviews"] = [
                dict(ts=n["ts"], reviewer=n["author"], verdict=m[1].lower(), notes=m[2])
                for n in t["notes"]
                if (
                    m := re.fullmatch(r"REVIEW (APPROVE|REJECT): (.*)", n["text"], re.S)
                )
            ]
        return t

    def tasks(self):
        counts = {
            r["task_id"]: r["n"]
            for r in self.s.q(
                "SELECT task_id,count(*) n FROM task_notes GROUP BY task_id"
            )
        }
        flags = {
            r["key"]: json.loads(r["value"])
            for r in self.s.q("SELECT * FROM kv WHERE key LIKE 'check_failed.%'")
        }
        return [
            self.task(t, counts=counts, flags=flags)
            for t in self.s.q("SELECT * FROM tasks ORDER BY priority,id")
        ]

    @staticmethod
    def message(row):
        return dict(row, room=None)

    @staticmethod
    def question(row):
        q = dict(row)
        if isinstance(q["options"], str):
            q["options"] = json.loads(q["options"])
        if q["kind"] == "safety":
            q["kind"] = "approval"
            q["approval"] = dict(task_id=q["task_id"], branch=None, paths=[])
        return q

    @staticmethod
    def memory(row):
        return dict(
            row,
            major=False,  # REQ-COM-034, not yet implemented (#27)
            pinned=bool(row["pinned"]),
            superseded_by=row["superseded_by"],
            status="superseded" if row["superseded_by"] else "active",
            comments_open=0,
        )

    def run(self, row):
        r = {k: v for k, v in row.items() if k not in ("prompt", "system", "cwd")}
        a = self.s.one("SELECT * FROM api_run_context WHERE run_id=?", r["id"]) or {}
        r.update(
            provider=a.get("provider", ""),
            model=a.get("model", ""),
            chat=bool(r["chat"]),
            lines=self.s.scalar(
                "SELECT count(*) FROM run_lines WHERE run_id=?", r["id"]
            ),
        )
        r["status"] = {"failed": "error", "stopped": "interrupted"}.get(
            r["status"], r["status"]
        )
        return r

    @staticmethod
    def line(row):
        return dict(seq=row["id"], ts=row["ts"], kind=row["kind"], text=row["text"])

    def engine_state(self):
        s = self.s
        stopped, paused = (
            bool(s.kv_get("stopped", False)),
            bool(s.kv_get("paused", False)),
        )
        throttled = s.kv_get("throttled") or None
        reloading = bool(s.kv_get("reloading", False))
        return dict(
            state="stopped"
            if stopped
            else "reloading"
            if reloading
            else "paused"
            if paused
            else "throttled"
            if throttled
            else "live",
            stopped=stopped,
            paused=paused,
            throttled=throttled,
            heartbeat=s.kv_get("heartbeat", 0),
            version=__version__,
            pid=os.getpid(),
            started_at=self.server.started_at,
            running_runs=s.scalar("SELECT count(*) FROM runs WHERE status='running'"),
            draining_runs=0,
            config_errors=[
                dict(file=r["key"][13:], message=json.loads(r["value"]))
                for r in s.q("SELECT * FROM kv WHERE key LIKE 'config_error.%'")
                if json.loads(r["value"])
            ],
            # #74/REQ-COM-013: how much the deterministic FYI rule saved in the last hour, for the
            # TUI header.
            fyi_wakes_avoided_1h=s.count_events_since("fyi_wake_avoided", time.time() - 3600),
            fyi_model_calls_avoided_1h=s.count_events_since("fyi_model_call_avoided", time.time() - 3600),
        )

    def usage(self):
        s = self.s
        providers = []
        for provider in ("claude", "codex", "local"):
            until = s.kv_get("limit." + provider) or 0
            reason = (s.kv_get(f"limit_meta.{provider}") or {}).get("reason", "provider")
            windows = []
            extra = {}
            if provider == "claude":
                raw = (s.kv_get("claude_ratelimit") or {}).get("unifiedWindows", {})
            elif provider == "codex":
                codex = s.kv_get("usage:codex") or {}
                raw = codex.get("unifiedWindows", {})
                if codex:
                    observed_at = codex.get("observed_at")
                    extra = dict(
                        plan=codex.get("plan_type"),
                        age=max(0, time.time() - observed_at) if observed_at else None,
                        source=codex.get("source"),
                    )
            else:
                raw = {}
            for key, window in raw.items():
                reset = (
                    window.get("resetsAt")
                    or window.get("resets_at")
                    or window.get("reset")
                )
                if isinstance(reset, str):
                    try:
                        reset = datetime.fromisoformat(
                            reset.replace("Z", "+00:00")
                        ).timestamp()
                    except ValueError:
                        reset = None
                # REQ-BE-016 MVP cap, per window (5h/7d each have their own); #38's provider_limits
                # (once configured) takes precedence over it.
                mvp_cap = claude_window_cap(self.cfg.budget, key) if provider == "claude" else 0
                windows.append(
                    dict(
                        name={"five_hour": "5h", "seven_day": "7d"}.get(key, key),
                        used_pct=float(window.get("utilization") or 0) * 100,
                        used_percent=window.get("used_percent"),
                        window_minutes=window.get("window_minutes"),
                        cap_pct=self.cfg.provider_limits.get(provider, {}).get(key) or (mvp_cap or None),
                        resets_at=reset,
                    )
                )
            capped = bool(until > time.time() and reason == "cap")
            providers.append(
                dict(
                    provider=provider,
                    limited_until=until if until > time.time() else None,
                    capped=capped,
                    windows=windows,
                    **extra,
                )
            )
        return dict(
            budget={
                k: v
                for k, v in asdict(self.cfg.budget).items()
                if k in ("max_runs_per_hour", "max_usd_per_day", "max_concurrent")
            },
            runs_1h=s.scalar(
                "SELECT count(*) FROM runs WHERE started>? AND chat=0",
                time.time() - 3600,
            ),
            cost_24h=s.scalar(
                "SELECT sum(cost) FROM runs WHERE started>?",
                time.time() - 86400,
                default=0,
            ),
            throttled=s.kv_get("throttled") or None,
            providers=providers,
        )

    def seen(self):
        return {k: self.s.kv_get(k) for k in ("human_last_seen", "decisions_seen_at")}

    def read(self, method, p):
        s = self.s
        if method == "snapshot":
            limit = integer(p, "messages_limit", 200, 1, 1000)
            return dict(
                seq=self.server.seq,
                server_time=time.time(),
                engine=self.engine_state(),
                usage=self.usage(),
                seen=self.seen(),
                agents=self.agents(),
                tasks=self.tasks(),
                milestones=s.milestones(),
                questions=[self.question(q) for q in s.questions(limit=1000000)],
                recent_messages=[self.message(m) for m in s.messages(limit)],
            )
        if method == "engine":
            return self.engine_state()
        if method == "usage":
            return self.usage()
        if method == "seen":
            return self.seen()
        if method == "config":
            return redact(
                dict(troupe_toml=self.cfg.toml_data, team_yaml=self.cfg.team_data)
            )
        if method == "agents":
            return dict(items=self.agents())
        if method == "milestones":
            return dict(items=s.milestones())
        if method == "task":
            return self.task(self.require("tasks", integer(p, "id", minimum=1)), True)
        if method == "tasks":
            status = p.get("status", list(ALL_STATUSES))
            if not isinstance(status, list) or any(
                x not in ALL_STATUSES for x in status
            ):
                raise APIError("bad_request", "status must be a list of task statuses")
            if p.get("milestone_id") is not None:
                self.require('milestones', integer(p, 'milestone_id', minimum=1))
            return dict(items=[t for t in self.tasks() if t['status'] in status and
                               ('milestone_id' not in p or t['milestone_id'] == p['milestone_id'])])
        if method == "questions":
            status = p.get("status", "open")
            if status not in ("open", "answered", "dismissed", "all"):
                raise APIError("bad_request", "invalid status")
            return dict(
                items=[
                    self.question(q)
                    for q in s.questions(
                        None if status == "all" else status,
                        integer(p, "limit", 50, 1, 1000),
                    )
                ]
            )
        if method == "decision_comments":
            self.require("memories", integer(p, "decision_id", minimum=1))
            return dict(items=[])
        if method == "memories":
            kind = p.get("kind", "")
            if kind not in ("", "decision", "note", "fact", "idea", "preference"):
                raise APIError("bad_request", "invalid kind")
            status = p.get("status", "active")
            if status not in ("active", "superseded", "reverted"):
                raise APIError("bad_request", "invalid status")
            if "major" in p:
                boolean(p, "major")
            if p.get("major") or status == "reverted":
                return dict(items=[])  # REQ-COM-034/036, not yet implemented (#27)
            rows = s.memories(kind=kind, limit=integer(p, "limit", 400, 1, 1000),
                              include_superseded=status == "superseded")
            if status == "superseded":
                rows = [r for r in rows if r["superseded_by"]]
            return dict(items=[self.memory(r) for r in rows])
        if method == "run_lines":
            rid = integer(p, "run_id", minimum=1)
            run = self.require("runs", rid)
            limit = integer(p, "limit", 500, 1, 2000)
            rows = s.run_lines(rid, integer(p, "after_seq", 0), limit + 1)
            more = len(rows) > limit
            rows = rows[:limit]
            return dict(
                items=[self.line(r) for r in rows],
                next_after_seq=rows[-1]["id"] if rows else p.get("after_seq", 0),
                done=bool(run["ended"]) and not more,
            )
        table = {"messages": "messages", "runs": "runs", "activity": "events"}[method]
        limit = integer(
            p,
            "limit",
            {"messages": 100, "runs": 30, "activity": 300}[method],
            1,
            200 if method == "runs" else 500,
        )
        where, args = ["1=1"], []
        if "before_id" in p:
            where.append("id<?")
            args.append(integer(p, "before_id", minimum=1))
        if "agent" in p:
            aid = self.agent_id(p["agent"])
            where.append(
                "(sender=? OR recipient=?)" if method == "messages" else "agent=?"
            )
            args.extend([aid, aid] if method == "messages" else [aid])
        if method == "messages":
            if "chat_with" in p:
                aid = self.agent_id(p["chat_with"])
                where.append(
                    "kind='chat' AND ((sender='human' AND recipient=?) OR (sender=? AND recipient='human'))"
                )
                args.extend([aid, aid])
            if "kind" in p:
                if p["kind"] not in ("msg", "chat", "system"):
                    raise APIError("bad_request", "invalid kind")
                where.append("kind=?")
                args.append(p["kind"])
            if "task_id" in p:
                tid = integer(p, "task_id", minimum=1)
                self.require("tasks", tid)
                where.append("task_id=?")
                args.append(tid)
            if "room" in p:
                if p["room"] != "team":
                    raise APIError("bad_request", "invalid room")
                where.append("0=1")
        if method == "activity" and boolean(p, "significant_only", False):
            where.append("significant=1")
        rows = s.q(
            f"SELECT * FROM {table} WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
            *args,
            limit + 1,
        )
        more = len(rows) > limit
        rows = rows[:limit]
        convert = (
            self.message
            if method == "messages"
            else self.run
            if method == "runs"
            else lambda r: dict(r, significant=bool(r["significant"]))
        )
        return dict(
            items=[convert(r) for r in rows],
            next_before_id=rows[-1]["id"] if more else None,
        )

    def task_fields(self, p):
        allowed = {
            "title",
            "description",
            "acceptance",
            "territory",
            "status",
            "priority",
            "role",
            "assignee",
            "depends_on",
            "milestone_id",
        }
        if not isinstance(p, dict) or p.keys() - allowed:
            raise APIError(
                "bad_request",
                f"fields: unknown field(s) {set(p) - allowed if isinstance(p, dict) else p}",
            )
        out = dict(p)
        for key in ("title", "description", "acceptance", "territory", "role"):
            if key in p:
                string(p, key)
        if "role" in p and p["role"] not in ROLES:
            raise APIError("bad_request", "unknown role")
        if "priority" in p:
            integer(p, "priority", minimum=0, maximum=3)
        if "status" in p and p["status"] not in ALL_STATUSES:
            raise APIError("bad_request", "invalid status")
        if p.get("assignee") is not None:
            out["assignee"] = self.agent_id(p["assignee"])
        if "depends_on" in p:
            if not isinstance(p["depends_on"], list):
                raise APIError("bad_request", "depends_on must be a list")
            for dep in p["depends_on"]:
                self.require("tasks", integer({"id": dep}, "id", minimum=1))
        if out.get("milestone_id") is not None:
            self.require("milestones", integer(out, "milestone_id", minimum=1))
        return out

    def command(self, method, p):
        s = self.s
        if method == 'milestone':
            action = p.get('action')
            if action not in ('create', 'update'):
                raise APIError('bad_request', 'action must be create or update')
            fields = {k: v for k, v in p.items() if k not in ('action', 'id')}
            for key in ('name', 'goal'):
                if key in fields:
                    string(fields, key)
            if 'name' in fields and not fields['name'].strip():
                raise APIError('bad_request', 'name must be non-empty')
            if 'order' in fields:
                integer(fields, 'order', minimum=-(2**63))
            if 'status' in fields and fields['status'] not in ('active', 'done'):
                raise APIError('bad_request', 'status must be active or done')
            if action == 'create':
                if 'id' in p:
                    raise APIError('bad_request', 'id is only valid for update')
                if not fields.get('name'):
                    raise APIError('bad_request', 'name is required')
                mid = s.add_milestone(**fields)
            else:
                mid = integer(p, 'id', minimum=1)
                self.require('milestones', mid)
                s.update_milestone(mid, **fields)
            return dict(milestone=s.milestone(mid))
        if method == "stop_team":
            return {"accepted": True}
        if method == "stop_now":
            from .safety import stop_now
            killed = s.scalar("SELECT count(*) FROM runs WHERE status='running'", default=0)
            stop_now(s)
            return dict(killed=killed)
        if method in STAGED:
            unavailable(*STAGED[method])
        if method in (
            "chat",
            "mark_chat_read",
            "wake",
            "stop_run",
            "set_agent_enabled",
            "new_session",
        ):
            aid = self.agent_id(p.get("agent"))
            if method == "chat":
                return dict(
                    message_id=s.send("human", aid, string(p, "text"), kind="chat")
                )
            if method == "mark_chat_read":
                count = s.conn.execute(
                    "UPDATE messages SET read_at=? WHERE sender=? AND recipient='human' AND kind='chat' AND read_at IS NULL",
                    (time.time(), aid),
                ).rowcount
                return dict(count=count)
            command = {
                "wake": "poke",
                "stop_run": "stop",
                "new_session": "reset_session",
                "set_agent_enabled": "enable" if p.get("enabled") else "disable",
            }[method]
            if method == "set_agent_enabled":
                boolean(p, "enabled")
            s.command(command, aid)
            if method == "set_agent_enabled":
                return dict(agent=next(a for a in self.agents() if a["id"] == aid))
            return dict(
                accepted=True,
                **(
                    {"run_id": s.agent(aid)["current_run"]}
                    if method == "stop_run"
                    else {}
                ),
            )
        if method in ("pause", "resume"):
            if method == "pause":
                s.kv_set("paused", True)
                s.event("human", "control", "You paused the troupe", significant=False)
            else:
                # Mirrors engine.handle_commands()'s "resume": clears both the plain pause
                # (REQ-ENG-014) and the kill switch (REQ-SAFE-010), which stop_now sets together, so
                # resuming always fully recovers regardless of which state the human is coming from.
                from .safety import audit
                s.kv_set("stopped", False)
                s.kv_set("paused", False)
                audit(s, "Human resumed the troupe", notify=False)
            return dict(engine=self.engine_state())
        if method in ("answer_question", "dismiss_question"):
            qid = integer(p, "id", minimum=1)
            q = self.require("questions", qid)
            if q["status"] != "open":
                raise APIError("conflict", "question is already answered or dismissed")
            if q["kind"] in ("approval", "safety"):
                if method == "dismiss_question":
                    raise APIError(
                        "forbidden", "approval questions cannot be dismissed"
                    )
                if p.get("decision") not in ("approve", "reject"):
                    raise APIError("bad_request", "decision must be approve or reject")
                # Same answer text shape as the raylib GUI's approval buttons (views.py
                # answer_question_option): "Approve"/"Reject" + an optional " — note", which is what
                # gates.py's verdict() parses to decide the outcome — one shared path, not a second.
                note = string(p, "text", "")
                label = "Approve" if p["decision"] == "approve" else "Reject"
                s.answer(qid, f"{label} — {note}" if note else label, status="answered")
                return dict(question=self.question(self.require("questions", qid)))
            if "decision" in p:
                raise APIError(
                    "bad_request", "decision only applies to approval questions"
                )
            s.answer(
                qid,
                string(p, "text")
                if method == "answer_question"
                else "(dismissed without an answer — use your judgment)",
                status="answered" if method == "answer_question" else "dismissed",
            )
            return dict(question=self.question(self.require("questions", qid)))
        if method == "create_task":
            fields = self.task_fields(
                {k: v for k, v in p.items() if k != "idempotency_key"}
            )
            string(p, "title")
            tid = s.add_task(**fields, status="ready", created_by="human")
            return dict(task=self.task(self.require("tasks", tid)))
        if method == "update_task":
            tid = integer(p, "id", minimum=1)
            t = self.require("tasks", tid)
            fields = self.task_fields(p.get("fields"))
            if fields.get("status") == "done" and t["branch"]:
                fields["status"] = "approved"
            if fields.get("status") == "approved":
                s.task_note(tid, "human", "Approved by the human.")
            if fields.get("status") == "ready":
                fields.update(attempts=0, next_attempt_at=0)
            s.update_task(
                tid,
                actor="human",
                event_text=f"You updated #{tid}: "
                + ", ".join(f"{k}={v}" for k, v in fields.items()),
                **fields,
            )
            return dict(task=self.task(self.require("tasks", tid)))
        if method == "add_task_note":
            tid = integer(p, "id", minimum=1)
            t = self.require("tasks", tid)
            text = string(p, "text")
            s.task_note(tid, "human", text)
            if t["assignee"]:
                s.send(
                    "human", t["assignee"], text, subject=f"Note on #{tid}", task_id=tid
                )
            else:
                s.event(
                    "human", "task", f"You added a note to #{tid}", ref=f"task:{tid}"
                )
            n = s.task_notes(tid)[-1]
            return dict(note=dict(id=n["id"], ts=n["ts"], author="human", text=text))
        if method in ("update_memory", "delete_memory"):
            mid = integer(p, "id", minimum=1)
            self.require("memories", mid)
            if method == "delete_memory":
                s.delete_memory(mid, actor="human")
                return dict(deleted=True)
            if "major" in p:
                unavailable("REQ-COM-034", 27)
            fields = {k: v for k, v in p.items() if k != "id"}
            if "pinned" in fields:
                boolean(fields, "pinned")
            for key in ("title", "content", "rationale"):
                if key in fields:
                    string(fields, key)
            if not fields:
                raise APIError("bad_request", "no fields to update")
            s.update_memory(mid, actor="human", **fields)
            return dict(memory=self.memory(self.require("memories", mid)))
        if method == "mark_seen":
            key = p.get("key")
            if key not in ("human_last_seen", "decisions_seen_at"):
                raise APIError("bad_request", "invalid key")
            ts = p.get("ts", time.time())
            if type(ts) not in (int, float) or not 0 <= ts < float("inf"):
                raise APIError("bad_request", "ts must be a finite timestamp")
            s.kv_set(key, max(s.kv_get(key, 0) or 0, ts))
            return dict(seen=self.seen())
        raise APIError("unknown_method", method)


class Connection:
    def __init__(self, server, reader, writer):
        self.server, self.reader, self.writer = server, reader, writer
        self.hello = False
        self.notifications = False
        self.topics = None
        self.subscribed = False
        self.queue = deque()
        self.queued = 0
        self.ready = asyncio.Event()
        self.closed = False

    def matches(self, event):
        return self.topics is None or any(
            event == t or (t.endswith(".*") and event.startswith(t[:-1]))
            for t in self.topics
        )

    def enqueue(self, obj, push=False, after_send=None):
        if self.closed:
            return
        raw = (
            json.dumps(
                bounded(obj), ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
            + "\n"
        ).encode()
        transport_bytes = self.writer.transport.get_write_buffer_size()
        if push and self.queued + transport_bytes + len(raw) > self.server.push_limit:
            self.queue = deque(
                (data, ispush, callback) for data, ispush, callback in self.queue if not ispush
            )
            self.queued = sum(len(data) for data, _, _ in self.queue)
            self.subscribed = False
            raw = (
                json.dumps(
                    dict(
                        event="resync_required",
                        seq=self.server.seq,
                        data=dict(reason="slow consumer"),
                    )
                )
                + "\n"
            ).encode()
            push = False
        if self.queued + transport_bytes + len(raw) > self.server.output_limit:
            self.close()
            return
        self.queue.append((raw, push, after_send))
        self.queued += len(raw)
        self.ready.set()

    def close(self):
        self.closed = True
        self.writer.close()
        self.ready.set()

    async def send(self):
        try:
            while not self.closed:
                await self.ready.wait()
                while self.queue:
                    raw, _, callback = self.queue.popleft()
                    self.queued -= len(raw)
                    self.writer.write(raw)
                    await self.writer.drain()
                    if callback:
                        callback()
                self.ready.clear()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            self.close()

    async def run(self):
        sender = asyncio.create_task(self.send())
        try:
            while not self.closed:
                try:
                    raw = await self.reader.readline()
                    if not raw:
                        break
                    if len(raw) > MAX_LINE:
                        raise ValueError("request line exceeds 4 MiB")
                    req = json.loads(
                        raw,
                        parse_constant=lambda _: (_ for _ in ()).throw(
                            ValueError("non-finite JSON number")
                        ),
                    )
                except (ValueError, UnicodeError, RecursionError):
                    self.enqueue(
                        dict(
                            id=None,
                            ok=False,
                            error=dict(
                                code="bad_request",
                                message="invalid JSON or request line exceeds 4 MiB",
                            ),
                        )
                    )
                    await self.flush()
                    break
                rid = req.get("id") if isinstance(req, dict) else None
                try:
                    result = self.server.request(self, req)
                    after_send = (lambda: self.server.data.s.command("stop_team")) if req["method"] == "stop_team" else None
                    self.enqueue(dict(id=rid, ok=True, result=result), after_send=after_send)
                except APIError as e:
                    self.enqueue(dict(id=rid, ok=False, error=e.object()))
                    if e.code == "unsupported_version":
                        await self.flush()
                        break
                except Exception:
                    self.server.log_exception("API request failed")
                    self.enqueue(
                        dict(
                            id=rid,
                            ok=False,
                            error=dict(
                                code="internal",
                                message="API request failed; see engine.log",
                            ),
                        )
                    )
        except (OSError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.close()
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            self.server.connections.discard(self)
            try:
                await self.writer.wait_closed()
            except (OSError, ConnectionError):
                pass

    async def flush(self):
        # Only protocol-fatal replies use this path; never wait on an unresponsive client forever.
        deadline = asyncio.get_running_loop().time() + 0.5
        while (
            self.queue
            and not self.closed
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.001)


class APIServer:
    def __init__(
        self, cfg, engine=None, *, push_limit=PUSH_LIMIT, output_limit=OUTPUT_LIMIT
    ):
        self.cfg, self.engine = cfg, engine
        self.epoch = uuid.uuid4().hex
        self.started_at = time.time()
        self.seq = 0
        self.ring = deque(maxlen=10000)
        self.connections = set()
        self.push_limit, self.output_limit = push_limit, output_limit
        self._ready = threading.Event()
        self._error = None
        self.loop = None
        self.path = None
        self._inode = None
        self._agent_state = {}
        self._agent_sent = {}
        self._engine_state = None
        self._usage = None
        self._usage_at = 0
        self._milestone_state = {}

    def log_exception(self, message: str) -> None:
        logging.exception(message)
        with (self.cfg.state_dir / "engine.log").open("a") as stream:
            stream.write(f"{time.time():.3f} {message}\n{traceback.format_exc()}\n")

    @property
    def notifications_suppressed(self):
        return any(c.notifications and not c.closed for c in tuple(self.connections))

    def start(self):
        self.thread = threading.Thread(
            target=self._thread, name="troupe-api", daemon=True
        )
        self.thread.start()
        if not self._ready.wait(1):
            raise RuntimeError("API did not start within one second")
        if self._error:
            raise self._error
        return self

    def stop(self):
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._stop.set)
        if hasattr(self, "thread"):
            self.thread.join(timeout=2)

    def _thread(self):
        try:
            asyncio.run(self._main())
        except Exception as e:
            self._error = e
            self._ready.set()
            self.log_exception("API server failed")
        finally:
            if (
                self.path
                and self._inode
                and self.path.exists()
                and self.path.stat().st_ino == self._inode
            ):
                self.path.unlink()

    def install_journal(self):
        s = self.data.s
        s.x(
            "CREATE TABLE IF NOT EXISTS api_changes(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, name TEXT, op TEXT, row TEXT)"
        )
        s.x(
            "CREATE TABLE IF NOT EXISTS api_run_context(run_id INTEGER PRIMARY KEY, provider TEXT, model TEXT)"
        )
        s.x("""CREATE TRIGGER IF NOT EXISTS api_run_context_insert AFTER INSERT ON runs BEGIN
            INSERT INTO api_run_context(run_id,provider,model)
            SELECT NEW.id,backend,model FROM agents WHERE id=NEW.agent;
            END""")
        # Triggers cover writers in other processes too. Payloads preserve rapid intermediate writes.
        for table in (
            "messages",
            "agents",
            "tasks",
            "task_notes",
            "questions",
            "memories",
            "runs",
            "run_lines",
            "events",
            "kv",
        ):
            columns = [r["name"] for r in s.q(f"PRAGMA table_info({table})")]
            for op in ("INSERT", "UPDATE", "DELETE"):
                alias = "OLD" if op == "DELETE" else "NEW"
                fields = ",".join(f"'{c}',{alias}.{c}" for c in columns)
                name = f"api_{table}_{op.lower()}"
                s.x(f"DROP TRIGGER IF EXISTS {name}")
                s.x(
                    f"CREATE TRIGGER {name} AFTER {op} ON {table} BEGIN INSERT INTO api_changes(ts,name,op,row) VALUES((julianday('now')-2440587.5)*86400,'{table}','{op}',json_object({fields})); END"
                )
        self.cursor = s.scalar("SELECT max(seq) FROM api_changes", default=0)

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self.path = socket_path(self.cfg.root, create=True)
        if self.path.exists():
            if not stat.S_ISSOCK(self.path.lstat().st_mode):
                raise RuntimeError("API path exists and is not a socket")
            probe = socket.socket(socket.AF_UNIX)
            probe.settimeout(0.2)
            try:
                probe.connect(str(self.path))
            except (ConnectionRefusedError, FileNotFoundError):
                self.path.unlink(missing_ok=True)
            else:
                raise RuntimeError("another API server owns this socket")
            finally:
                probe.close()
        # Bind under a restrictive umask before accepting any connection.
        sock = socket.socket(socket.AF_UNIX)
        old = os.umask(0o177)
        try:
            sock.bind(str(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        self._inode = self.path.stat().st_ino
        sock.listen()
        sock.setblocking(False)
        self.data = Data(self)
        self.install_journal()
        self._milestone_state = {m["id"]: m for m in self.data.s.milestones()}
        self._agent_state = {a["id"]: a for a in self.data.agents()}
        self._engine_state = self.data.engine_state()
        self._usage = self.data.usage()
        server = await asyncio.start_unix_server(
            self.accept, sock=sock, limit=MAX_LINE + 1
        )
        from .service import write_service_state

        write_service_state(self.cfg.state_dir, "running")
        self._ready.set()
        try:
            while not self._stop.is_set():
                self.collect()
                try:
                    await asyncio.wait_for(self._stop.wait(), 0.05)
                except TimeoutError:
                    pass
        finally:
            server.close()
            await server.wait_closed()
            for c in tuple(self.connections):
                c.close()
            if self.path.exists() and self.path.stat().st_ino == self._inode:
                self.path.unlink()
            write_service_state(self.cfg.state_dir, "stopped")
            self.data.s.conn.close()

    async def accept(self, reader, writer):
        c = Connection(self, reader, writer)
        self.connections.add(c)
        await c.run()

    def publish(self, event, data):
        self.seq += 1
        value = dict(event=event, seq=self.seq, data=bounded(data))
        stamp = time.time()
        self.ring.append((stamp, value))
        while self.ring and self.ring[0][0] < stamp - 600:
            self.ring.popleft()
        for c in tuple(self.connections):
            if c.subscribed and c.matches(event):
                c.enqueue(value, push=True)

    def collect(self):
        d, s = self.data, self.data.s
        rows = s.q("SELECT * FROM api_changes WHERE seq>? ORDER BY seq", self.cursor)
        for change in rows:
            self.cursor = change["seq"]
            table, op = change["name"], change["op"]
            r = json.loads(change["row"])
            if table == "messages" and op != "DELETE":
                self.publish("message.new", dict(message=d.message(r)))
            elif table == "tasks" and op != "DELETE":
                self.publish("task.changed", dict(task=d.task(r)))
            elif table == "task_notes":
                t = s.task(r["task_id"])
                if t:
                    self.publish("task.changed", dict(task=d.task(t)))
            elif table == "questions" and op != "DELETE":
                self.publish(
                    "question.new" if op == "INSERT" else "question.answered",
                    dict(question=d.question(r)),
                )
            elif table == "memories":
                self.publish(
                    "memory.new" if op == "INSERT" else "memory.changed",
                    dict(
                        memory=dict(id=r["id"], deleted=True)
                        if op == "DELETE"
                        else d.memory(r)
                    ),
                )
            elif table == "runs" and op != "DELETE":
                self.publish(
                    "run.started" if r["status"] == "running" else "run.finished",
                    dict(run=d.run(r)),
                )
            elif table == "run_lines" and op == "INSERT":
                run = s.one("SELECT agent FROM runs WHERE id=?", r["run_id"])
                if run:
                    self.publish(
                        "run.output",
                        dict(run_id=r["run_id"], agent=run["agent"], line=d.line(r)),
                    )
            elif table == "events" and op == "INSERT":
                self.publish(
                    "activity.new",
                    dict(activity=dict(r, significant=bool(r["significant"]))),
                )
            elif table == "kv":
                if r["key"] in ("human_last_seen", "decisions_seen_at"):
                    self.publish("seen.changed", dict(seen=d.seen()))
                if r["key"].startswith("check_failed."):
                    t = s.task(int(r["key"].split(".")[1]))
                    if t:
                        self.publish("task.changed", dict(task=d.task(t)))
        stamp = time.time()
        while self.ring and self.ring[0][0] < stamp - 600:
            self.ring.popleft()
        for a in d.agents():
            if (
                a != self._agent_state.get(a["id"])
                and stamp - self._agent_sent.get(a["id"], 0) >= 0.25
            ):
                self._agent_state[a["id"]] = a
                self._agent_sent[a["id"]] = stamp
                self.publish("agent.state", dict(agent=a))
        for milestone in s.milestones():
            if milestone != self._milestone_state.get(milestone['id']):
                self._milestone_state[milestone['id']] = milestone
                self.publish('milestone.changed', dict(milestone=milestone))
        eng = d.engine_state()
        if {k: v for k, v in eng.items() if k != "heartbeat"} != {
            k: v for k, v in self._engine_state.items() if k != "heartbeat"
        }:
            self._engine_state = eng
            self.publish("engine.state", dict(engine=eng))
        if stamp - self._usage_at >= 1:
            usage = d.usage()
            self._usage_at = stamp
            if usage != self._usage:
                self._usage = usage
                self.publish("usage.changed", dict(usage=usage))
        if rows and not s.conn.in_transaction:
            s.x(
                "DELETE FROM api_changes WHERE seq<=? AND ts<?",
                self.cursor,
                stamp - 600,
            )

    def request(self, c, req):
        if (
            not isinstance(req, dict)
            or type(req.get("id")) not in (int, str)
            or not isinstance(req.get("method"), str)
        ):
            raise APIError("bad_request", "request requires id and method")
        if req.keys() - {"id", "method", "params"}:
            raise APIError("bad_request", "unknown request field")
        method, p = req["method"], req.get("params", {})
        if method not in ("ping", "hello") and not c.hello:
            raise APIError("handshake_required", "call hello first")
        if method not in PARAMS:
            raise APIError("unknown_method", method)
        if not isinstance(p, dict) or p.keys() - set(PARAMS[method].split()):
            raise APIError("bad_request", f"unknown or invalid params for {method}")
        if method == "ping":
            return dict(pong=True, server_time=time.time())
        if method == "hello":
            if integer(p, "api_version") != 0:
                raise APIError(
                    "unsupported_version",
                    "unsupported API version",
                    dict(supported=[0]),
                )
            string(p, "client")
            c.notifications = boolean(p, "notifications", False)
            c.hello = True
            return dict(
                api_version=0,
                project=self.data.cfg.project,
                handle_suffix="@"
                + HandleBook(self.data.cfg.project, self.data.cfg.agents).project,
                root=str(self.cfg.root),
                troupe_version=__version__,
                epoch=self.epoch,
                seq=self.seq,
                server_time=time.time(),
                engine=self.data.engine_state(),
            )
        if method == "unsubscribe":
            c.subscribed = False
            return dict(epoch=self.epoch, seq=self.seq)
        if method == "subscribe":
            self.collect()
            topics = p.get("topics")
            if topics is not None and (
                not isinstance(topics, list)
                or any(
                    not isinstance(t, str)
                    or not re.fullmatch(r"[a-z_]+\.(?:[a-z_]+|\*)|resync_required", t)
                    for t in topics
                )
            ):
                raise APIError(
                    "bad_request",
                    "topics must contain event names or prefixes ending .*",
                )
            since = integer(p, "since_seq", self.seq)
            floor = self.ring[0][1]["seq"] - 1 if self.ring else self.seq
            if since < floor or since > self.seq:
                raise APIError(
                    "resync_required", "event history is no longer available"
                )
            c.topics = topics
            c.subscribed = True
            for _, event in self.ring:
                if event["seq"] > since and c.subscribed and c.matches(event["event"]):
                    c.enqueue(event, push=True)
            return dict(epoch=self.epoch, seq=self.seq)
        if method in HUMAN_ONLY_METHODS and caller_is_agent(c.writer, self.engine):
            raise APIError("forbidden", "only the human can do this; use ask_human instead")
        s = self.data.s
        s.conn.execute("BEGIN IMMEDIATE" if method in COMMANDS else "BEGIN")
        try:
            if method in COMMANDS:
                key = p.get("idempotency_key")
                if key is not None:
                    if not isinstance(key, str) or not 1 <= len(key) <= 64:
                        raise APIError(
                            "bad_request",
                            "idempotency_key must contain 1..64 characters",
                        )
                    previous = s.one(
                        "SELECT result FROM api_idempotency WHERE method=? AND key=? AND ts>?",
                        method,
                        key,
                        time.time() - 600,
                    )
                    if previous:
                        result = dict(json.loads(previous["result"]), duplicate=True)
                        s.conn.commit()
                        return result
                result = self.data.command(method, p)
                if key is not None:
                    s.x("DELETE FROM api_idempotency WHERE ts<?", time.time() - 600)
                    s.x(
                        "INSERT OR REPLACE INTO api_idempotency VALUES(?,?,?,?)",
                        method,
                        key,
                        time.time(),
                        json.dumps(result),
                    )
            else:
                # The journal and snapshot use one SQLite read view. Later commits replay after this seq.
                self.collect()
                result = self.data.read(method, p)
            s.conn.commit()
            return result
        except Exception:
            s.conn.rollback()
            raise
