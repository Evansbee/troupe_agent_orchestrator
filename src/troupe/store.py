"""SQLite-backed shared state. Every process (engine, GUI, per-agent MCP servers) talks through this."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, role TEXT, name TEXT, backend TEXT, model TEXT,
  state TEXT DEFAULT 'idle', status TEXT DEFAULT '', activity TEXT DEFAULT '',
  session_id TEXT, enabled INTEGER DEFAULT 1, last_run_at REAL DEFAULT 0,
  last_event_seen INTEGER DEFAULT 0, runs INTEGER DEFAULT 0, cost REAL DEFAULT 0,
  tokens INTEGER DEFAULT 0, current_run INTEGER, session_runs INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, sender TEXT, recipient TEXT,
  subject TEXT DEFAULT '', body TEXT, kind TEXT DEFAULT 'msg', reply_to INTEGER,
  task_id INTEGER, read_at REAL
);
CREATE INDEX IF NOT EXISTS messages_inbox ON messages(recipient, read_at);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, updated REAL, title TEXT,
  description TEXT DEFAULT '', acceptance TEXT DEFAULT '', territory TEXT DEFAULT '',
  status TEXT DEFAULT 'backlog', priority INTEGER DEFAULT 2, role TEXT DEFAULT 'builder',
  assignee TEXT, reviewer TEXT, created_by TEXT, depends_on TEXT DEFAULT '[]',
  branch TEXT, worktree TEXT, attempts INTEGER DEFAULT 0, next_attempt_at REAL DEFAULT 0,
  result TEXT DEFAULT '', review_notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS task_notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, ts REAL, agent TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, asker TEXT, kind TEXT DEFAULT 'question',
  question TEXT, context TEXT DEFAULT '', options TEXT DEFAULT '[]', status TEXT DEFAULT 'open',
  answer TEXT, answered_at REAL, task_id INTEGER
);
CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, agent TEXT, kind TEXT DEFAULT 'decision',
  title TEXT, content TEXT DEFAULT '', rationale TEXT DEFAULT '', scope TEXT DEFAULT 'team'
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, agent TEXT, kind TEXT, text TEXT,
  ref TEXT DEFAULT '', significant INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT, started REAL, ended REAL, reason TEXT,
  status TEXT DEFAULT 'running', cost REAL DEFAULT 0, tokens INTEGER DEFAULT 0,
  task_id INTEGER, cwd TEXT, summary TEXT DEFAULT '', chat INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS run_lines(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts REAL, kind TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS run_lines_run ON run_lines(run_id, id);
CREATE TABLE IF NOT EXISTS commands(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, cmd TEXT, arg TEXT DEFAULT '', done INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""

OPEN_STATUSES = ("backlog", "ready", "in_progress", "blocked", "review", "approved")
ALL_STATUSES = OPEN_STATUSES + ("done", "cancelled")


def now() -> float:
    return time.time()


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()
        self.conn.executescript(SCHEMA)

    # ── plumbing ──────────────────────────────────────────────────────────
    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    def q(self, sql: str, *args: Any) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, *args: Any) -> dict | None:
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def x(self, sql: str, *args: Any) -> int:
        return self.conn.execute(sql, args).lastrowid

    def scalar(self, sql: str, *args: Any, default: Any = None) -> Any:
        r = self.conn.execute(sql, args).fetchone()
        return r[0] if r and r[0] is not None else default

    # ── kv ────────────────────────────────────────────────────────────────
    def kv_get(self, key: str, default: Any = None) -> Any:
        v = self.scalar("SELECT value FROM kv WHERE key=?", key)
        return json.loads(v) if v is not None else default

    def kv_set(self, key: str, value: Any) -> None:
        self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               key, json.dumps(value))

    # ── agents ────────────────────────────────────────────────────────────
    def sync_agents(self, agents: list) -> None:
        for a in agents:
            self.x("""INSERT INTO agents(id,role,name,backend,model,enabled) VALUES(?,?,?,?,?,?)
                      ON CONFLICT(id) DO UPDATE SET role=excluded.role, name=excluded.name,
                      backend=excluded.backend, model=excluded.model""",
                   a.id, a.role, a.name, a.backend, a.model, int(a.enabled))
        ids = [a.id for a in agents]
        self.x(f"DELETE FROM agents WHERE id NOT IN ({','.join('?' * len(ids))})", *ids)

    def agents(self) -> list[dict]:
        return self.q("SELECT * FROM agents ORDER BY rowid")

    def agent(self, agent_id: str) -> dict | None:
        return self.one("SELECT * FROM agents WHERE id=?", agent_id)

    def set_agent(self, agent_id: str, **fields: Any) -> None:
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self.x(f"UPDATE agents SET {cols} WHERE id=?", *fields.values(), agent_id)

    # ── events (activity feed) ────────────────────────────────────────────
    def event(self, agent: str, kind: str, text: str, ref: str = "", significant: bool = True) -> int:
        return self.x("INSERT INTO events(ts,agent,kind,text,ref,significant) VALUES(?,?,?,?,?,?)",
                      now(), agent, kind, text, ref, int(significant))

    def events(self, limit: int = 200, after: int = 0) -> list[dict]:
        return self.q("SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT ?", after, limit)

    def max_event_id(self) -> int:
        return self.scalar("SELECT MAX(id) FROM events", default=0)

    def world_changed_for(self, agent_id: str, since: int) -> bool:
        return bool(self.scalar(
            "SELECT 1 FROM events WHERE id>? AND significant=1 AND agent!=? LIMIT 1", since, agent_id))

    # ── messages ──────────────────────────────────────────────────────────
    def send(self, sender: str, recipient: str, body: str, subject: str = "", kind: str = "msg",
             reply_to: int | None = None, task_id: int | None = None) -> int:
        mid = self.x("""INSERT INTO messages(ts,sender,recipient,subject,body,kind,reply_to,task_id)
                        VALUES(?,?,?,?,?,?,?,?)""", now(), sender, recipient, subject, body, kind, reply_to, task_id)
        label = subject or (body.strip().splitlines() or [""])[0]
        self.event(sender, "message", f"{sender} → {recipient}: {label[:120]}", ref=f"msg:{mid}")
        return mid

    def unread(self, recipient: str) -> list[dict]:
        return self.q("SELECT * FROM messages WHERE recipient=? AND read_at IS NULL ORDER BY id", recipient)

    def mark_read(self, ids: list[int]) -> None:
        if ids:
            self.x(f"UPDATE messages SET read_at=? WHERE id IN ({','.join('?' * len(ids))})", now(), *ids)

    def mark_unread(self, ids: list[int]) -> None:
        if ids:
            self.x(f"UPDATE messages SET read_at=NULL WHERE id IN ({','.join('?' * len(ids))})", *ids)

    def messages(self, limit: int = 500) -> list[dict]:
        return self.q("SELECT * FROM messages ORDER BY id DESC LIMIT ?", limit)

    def chat_thread(self, agent_id: str, limit: int = 300) -> list[dict]:
        return list(reversed(self.q(
            """SELECT * FROM messages WHERE (sender='human' AND recipient=?) OR (sender=? AND recipient='human')
               ORDER BY id DESC LIMIT ?""", agent_id, agent_id, limit)))

    # ── tasks ─────────────────────────────────────────────────────────────
    def task(self, task_id: int) -> dict | None:
        t = self.one("SELECT * FROM tasks WHERE id=?", task_id)
        if t:
            t["depends_on"] = json.loads(t["depends_on"] or "[]")
        return t

    def tasks(self, statuses: tuple[str, ...] | None = None, limit: int = 1000) -> list[dict]:
        if statuses:
            rows = self.q(f"SELECT * FROM tasks WHERE status IN ({','.join('?' * len(statuses))}) "
                          "ORDER BY priority, id LIMIT ?", *statuses, limit)
        else:
            rows = self.q("SELECT * FROM tasks ORDER BY priority, id LIMIT ?", limit)
        for t in rows:
            t["depends_on"] = json.loads(t["depends_on"] or "[]")
        return rows

    def add_task(self, title: str, description: str = "", acceptance: str = "", territory: str = "",
                 status: str = "backlog", priority: int = 2, role: str = "builder", assignee: str | None = None,
                 created_by: str = "human", depends_on: list[int] | None = None) -> int:
        tid = self.x("""INSERT INTO tasks(created,updated,title,description,acceptance,territory,status,priority,
                        role,assignee,created_by,depends_on) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     now(), now(), title, description, acceptance, territory, status, priority, role, assignee,
                     created_by, json.dumps(depends_on or []))
        self.event(created_by, "task", f"{created_by} created #{tid} {title} [{status}]", ref=f"task:{tid}")
        return tid

    def update_task(self, task_id: int, actor: str = "", event_text: str = "", significant: bool = True,
                    **fields: Any) -> None:
        if "depends_on" in fields and not isinstance(fields["depends_on"], str):
            fields["depends_on"] = json.dumps(fields["depends_on"])
        fields["updated"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE tasks SET {cols} WHERE id=?", *fields.values(), task_id)
        if event_text:
            self.event(actor or "system", "task", event_text, ref=f"task:{task_id}", significant=significant)

    def task_note(self, task_id: int, agent: str, text: str) -> None:
        self.x("INSERT INTO task_notes(task_id,ts,agent,text) VALUES(?,?,?,?)", task_id, now(), agent, text)

    def task_notes(self, task_id: int) -> list[dict]:
        return self.q("SELECT * FROM task_notes WHERE task_id=? ORDER BY id", task_id)

    # ── questions for the human ───────────────────────────────────────────
    def ask(self, asker: str, question: str, context: str = "", options: list[str] | None = None,
            kind: str = "question", task_id: int | None = None) -> int:
        qid = self.x("""INSERT INTO questions(ts,asker,kind,question,context,options,task_id)
                        VALUES(?,?,?,?,?,?,?)""", now(), asker, kind, question, context,
                     json.dumps(options or []), task_id)
        self.event(asker, "question", f"{asker} asks you: {question[:140]}", ref=f"q:{qid}")
        return qid

    def questions(self, status: str | None = "open", limit: int = 200) -> list[dict]:
        if status:
            rows = self.q("SELECT * FROM questions WHERE status=? ORDER BY id DESC LIMIT ?", status, limit)
        else:
            rows = self.q("SELECT * FROM questions ORDER BY id DESC LIMIT ?", limit)
        for r in rows:
            r["options"] = json.loads(r["options"] or "[]")
        return rows

    def answer(self, qid: int, answer: str, status: str = "answered") -> None:
        qn = self.one("SELECT * FROM questions WHERE id=?", qid)
        if not qn or qn["status"] != "open":
            return
        self.x("UPDATE questions SET status=?, answer=?, answered_at=? WHERE id=?", status, answer, now(), qid)
        label = "Idea" if qn["kind"] == "idea" else "Question"
        verb = "dismissed" if status == "dismissed" else "answered"
        body = f"The human {verb} your {label.lower()} #{qid}.\n\n> {qn['question']}\n\nAnswer: {answer}"
        self.send("human", qn["asker"], body, subject=f"{label} #{qid} {verb}", task_id=qn["task_id"])
        self.event("human", "answer", f"You {verb} {qn['asker']}'s {label.lower()}: {answer[:100]}", ref=f"q:{qid}")

    # ── memory ────────────────────────────────────────────────────────────
    def remember(self, agent: str, title: str, content: str = "", rationale: str = "", kind: str = "decision",
                 scope: str = "team") -> int:
        mid = self.x("INSERT INTO memories(ts,agent,kind,title,content,rationale,scope) VALUES(?,?,?,?,?,?,?)",
                     now(), agent, kind, title, content, rationale, scope)
        self.event(agent, "memory", f"{agent} recorded {kind}: {title[:120]}", ref=f"mem:{mid}",
                   significant=scope == "team")
        return mid

    def memories(self, agent: str | None = None, query: str = "", kind: str = "", limit: int = 50,
                 include_private_of: str | None = None) -> list[dict]:
        sql = "SELECT * FROM memories WHERE 1=1"
        args: list[Any] = []
        if agent:
            sql += " AND agent=?"
            args.append(agent)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if include_private_of is not None:
            sql += " AND (scope='team' OR agent=?)"
            args.append(include_private_of)
        for word in query.split():
            sql += " AND (title LIKE ? OR content LIKE ? OR rationale LIKE ?)"
            args += [f"%{word}%"] * 3
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return self.q(sql, *args)

    # ── runs ──────────────────────────────────────────────────────────────
    def start_run(self, agent: str, reason: str, task_id: int | None, cwd: str, chat: bool) -> int:
        return self.x("INSERT INTO runs(agent,started,reason,task_id,cwd,chat) VALUES(?,?,?,?,?,?)",
                      agent, now(), reason, task_id, cwd, int(chat))

    def end_run(self, run_id: int, status: str, cost: float, tokens: int, summary: str) -> None:
        self.x("UPDATE runs SET ended=?, status=?, cost=?, tokens=?, summary=? WHERE id=?",
               now(), status, cost, tokens, summary, run_id)

    def run_line(self, run_id: int, kind: str, text: str) -> None:
        self.x("INSERT INTO run_lines(run_id,ts,kind,text) VALUES(?,?,?,?)", run_id, now(), kind, text)

    def run_lines(self, run_id: int, after: int = 0, limit: int = 2000) -> list[dict]:
        return self.q("SELECT * FROM run_lines WHERE run_id=? AND id>? ORDER BY id LIMIT ?", run_id, after, limit)

    def runs(self, agent: str | None = None, limit: int = 30) -> list[dict]:
        if agent:
            return self.q("SELECT * FROM runs WHERE agent=? ORDER BY id DESC LIMIT ?", agent, limit)
        return self.q("SELECT * FROM runs ORDER BY id DESC LIMIT ?", limit)

    # ── commands (GUI/CLI → engine) ───────────────────────────────────────
    def command(self, cmd: str, arg: str = "") -> None:
        self.x("INSERT INTO commands(ts,cmd,arg) VALUES(?,?,?)", now(), cmd, arg)

    def pending_commands(self) -> list[dict]:
        rows = self.q("SELECT * FROM commands WHERE done=0 ORDER BY id")
        if rows:
            self.x(f"UPDATE commands SET done=1 WHERE id IN ({','.join('?' * len(rows))})", *[r["id"] for r in rows])
        return rows
