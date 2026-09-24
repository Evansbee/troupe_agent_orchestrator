"""GUI-side view of the shared state: periodic snapshots of the DB plus human actions."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from ..config import Config
from ..roles import get_role
from ..store import OPEN_STATUSES, Store, HandleBook


class Data:
    REFRESH = 0.35

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.names = HandleBook(cfg.project, cfg.agents)
        self.last = 0.0
        self.agents: list[dict] = []
        self.agent_by_id: dict[str, dict] = {}
        self.tasks: list[dict] = []
        self.questions: list[dict] = []
        self.events: list[dict] = []
        self.messages: list[dict] = []
        self.memories: list[dict] = []
        self.chat_unread: dict[str, int] = {}
        self.kv: dict = {}
        self.cost_24h = 0.0
        self.runs_1h = 0
        self.work_tokens_1h = {"coordination": 0, "work": 0}
        self.new_messages: list[dict] = []
        self.new_questions: list[dict] = []
        self.new_chat_answers: list[dict] = []
        self._answer_event = None
        self._max_msg = -1
        self._max_q = -1
        self.docs: list[Path] = []
        self._docs_at = 0.0

    def role_of(self, agent_id: str):
        a = self.agent_by_id.get(agent_id)
        return get_role(a["role"]) if a else None

    def color_of(self, agent_id: str) -> tuple:
        from . import theme as T

        if agent_id == "human":
            return T.HUMAN
        if agent_id == "system":
            return T.TEXT_FAINT
        r = self.role_of(agent_id)
        return (*r.color, 255) if r else T.TEXT_DIM

    def initials_of(self, agent_id: str) -> str:
        a = self.agent_by_id.get(agent_id)
        if not a:
            return "YOU" if agent_id == "human" else agent_id[:2].upper()
        r = get_role(a["role"])
        if r.key == "builder":
            digits = "".join(ch for ch in agent_id if ch.isdigit())
            return f"B{digits or ''}"
        return r.initials

    def name_of(self, agent_id: str, *, local: bool = False) -> str:
        if agent_id == "human":
            return "You"
        return self.names.name(agent_id, local=local)

    def refresh(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last < self.REFRESH:
            return
        self.last = now
        s = self.store
        self.agents = s.agents()
        self.names = HandleBook(self.cfg.project, self.agents)
        self.agent_by_id = {a["id"]: a for a in self.agents}
        self.tasks = s.tasks(limit=800)
        checking = s.kv_get("checking_task")
        failed = {r["key"] for r in s.q("SELECT key FROM kv WHERE key LIKE 'check_failed.%' AND value='true'")}
        for task in self.tasks:
            task["merge_check"] = ("checking…" if task["id"] == checking else
                                   "checks failed" if f"check_failed.{task['id']}" in failed else "")
        self.questions = s.questions("open")
        self.events = s.events(limit=300)
        for event in self.events:
            event["text"] = self.names.event_text(event["text"])
        answer_event = s.max_event_id()
        if self._answer_event is not None:
            self.new_chat_answers += s.q(
                "SELECT q.* FROM events e JOIN questions q ON e.ref='q:' || q.id "
                "WHERE e.id>? AND e.id<=? AND e.kind='answer' AND q.answered_via='chat' ORDER BY e.id",
                self._answer_event, answer_event)
        self._answer_event = answer_event
        totals = s.q("SELECT CASE WHEN task_id IS NOT NULL OR reason IN ('task','review') THEN 'work' "
                     "ELSE 'coordination' END AS category, SUM(tokens) AS tokens FROM runs "
                     "WHERE started>? AND chat=0 GROUP BY category", now - 3600)
        self.work_tokens_1h = {"coordination": 0, "work": 0}
        self.work_tokens_1h.update({r["category"]: r["tokens"] or 0 for r in totals})
        self.messages = s.messages(limit=600)
        self.memories = s.memories(limit=400)
        self.kv = {k: s.kv_get(k) for k in ("paused", "heartbeat", "throttled", "claude_ratelimit",
                                                       "limit.claude", "limit.codex", "limit.local",
                                                       "config_error.team.yaml", "config_error.troupe.toml")}
        self.cost_24h = s.scalar("SELECT SUM(cost) FROM runs WHERE started>?", now - 86400, default=0.0)
        self.runs_1h = s.scalar("SELECT COUNT(*) FROM runs WHERE started>? AND chat=0", now - 3600, default=0)
        self.chat_unread = {r["sender"]: r["n"] for r in s.q(
            "SELECT sender, COUNT(*) n FROM messages WHERE recipient='human' AND read_at IS NULL GROUP BY sender")}
        top = self.messages[0]["id"] if self.messages else 0
        if self._max_msg >= 0:
            self.new_messages = [m for m in reversed(self.messages) if m["id"] > self._max_msg]
        self._max_msg = top
        qtop = max((q["id"] for q in self.questions), default=0)
        if self._max_q >= 0:
            self.new_questions = [q for q in self.questions if q["id"] > self._max_q]
        self._max_q = max(self._max_q, qtop)
        if now - self._docs_at > 3:
            self._docs_at = now
            self.docs = self.scan_docs()

    # ── derived ───────────────────────────────────────────────────────────
    def limit_label(self, backend: str) -> str:
        until = self.kv.get(f"limit.{backend}") or 0
        if until <= time.time():
            return ""
        reset = time.strftime("%H:%M", time.localtime(until))
        return f"{backend.title()} limited until {reset}"

    @property
    def engine_alive(self) -> bool:
        hb = self.kv.get("heartbeat") or 0
        return time.time() - hb < 5

    @property
    def paused(self) -> bool:
        return bool(self.kv.get("paused"))

    def running_count(self) -> int:
        return sum(1 for a in self.agents if a["state"] == "running")

    def open_tasks_of(self, agent_id: str) -> list[dict]:
        return [t for t in self.tasks if t["assignee"] == agent_id and t["status"] in OPEN_STATUSES]

    def parked(self, agent: dict) -> bool:
        """OpenRig-style derived diagnosis: idle while owing work that isn't waiting on anyone."""
        if agent["state"] == "running" or not agent["enabled"]:
            return False
        owed = [t for t in self.open_tasks_of(agent["id"]) if t["status"] in ("in_progress", "ready")]
        return bool(owed) and time.time() - (agent["last_run_at"] or 0) > 180

    def chat_thread(self, agent_id: str) -> list[dict]:
        return self.store.chat_thread(agent_id)

    def scan_docs(self) -> list[Path]:
        root = self.cfg.root
        out: list[Path] = []
        for p in sorted(root.glob("*.md")):
            out.append(p)
        for d in ("specs", "design", "docs"):
            base = root / d
            if base.is_dir():
                out += sorted(x for x in base.rglob("*.md") if ".troupe" not in x.parts)
        return out

    # ── actions ───────────────────────────────────────────────────────────
    def send_chat(self, agent_id: str, text: str) -> None:
        self.store.send("human", agent_id, text, kind="chat")
        self.refresh(force=True)

    def mark_chat_read(self, agent_id: str) -> None:
        if self.chat_unread.get(agent_id):
            self.store.x("UPDATE messages SET read_at=? WHERE recipient='human' AND sender=? AND read_at IS NULL",
                         time.time(), agent_id)
            self.chat_unread[agent_id] = 0

    def answer(self, qid: int, text: str) -> None:
        self.store.answer(qid, text)
        self.refresh(force=True)

    def dismiss(self, qid: int) -> None:
        self.store.answer(qid, "(dismissed without an answer — use your judgment)", status="dismissed")
        self.refresh(force=True)

    def set_paused(self, paused: bool) -> None:
        self.store.kv_set("paused", paused)
        self.store.event("human", "control", "You paused the troupe" if paused else "You resumed the troupe",
                         significant=False)
        self.refresh(force=True)

    def command(self, cmd: str, arg: str = "") -> None:
        self.store.command(cmd, arg)

    def set_task(self, task_id: int, **fields) -> None:
        desc = ", ".join(f"{k}={v}" for k, v in fields.items())
        self.store.update_task(task_id, actor="human", event_text=f"You updated #{task_id}: {desc}", **fields)
        self.refresh(force=True)

    def add_task(self, title: str, description: str, role: str = "builder", priority: int = 2) -> int:
        tid = self.store.add_task(title, description, status="ready", priority=priority, role=role, created_by="human")
        self.refresh(force=True)
        return tid

    def notify(self, title: str, body: str) -> None:
        """macOS notification (best effort)."""
        safe_t = title.replace('"', "'")[:80]
        safe_b = body.replace('"', "'").replace("\n", " ")[:180]
        try:
            subprocess.Popen(["osascript", "-e", f'display notification "{safe_b}" with title "{safe_t}"'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
