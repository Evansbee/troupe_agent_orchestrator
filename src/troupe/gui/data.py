"""GUI-side view of the shared state: periodic snapshots of the DB plus human actions."""

from __future__ import annotations

import time
import threading
from pathlib import Path

from ..config import Config
from ..roles import get_role
from ..store import OPEN_STATUSES, Store, HandleBook

RUNS_PAGE = 200  # size of the recent-runs window refreshed every cycle, and of each "load older" page


class Data:
    REFRESH = 0.35

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.names = HandleBook(cfg.project, cfg.agents)
        self.catchup = {}
        self.catchup_cost = 0.0
        self.catchup_detail = None
        self._focused = False
        self._focus_at = 0.0
        self.new_help = []
        self._help_event = None
        self._seen_at = 0.0
        self.service_action = None
        self.service_error = ""
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
        self._run_targets: dict[int, str] = {}
        self._runs_cache: dict[str, dict[int, dict]] = {}  # agent_id -> {run_id: row}; merge-only, never evicted
        self._runs_watched: set[str] = set()  # agents whose run history has been viewed; kept fresh by refresh()
        self._runs_exhausted: dict[str, bool] = {}  # agent_id -> True once its oldest run is loaded
        self._runs_older_limit: dict[str, int] = {}  # agent_id -> deepest "load older" depth actually fetched
        self._runs_older_pending: dict[str, int] = {}  # agent_id -> depth queued by load_older_runs, not yet fetched

    def human_seen(self) -> None:
        stamp = time.time()
        self.store.kv_set("human_last_seen", max(stamp, self.store.kv_get("human_last_seen", 0) or 0))
        self._seen_at = stamp

    def dismiss_catchup(self) -> None:
        self.catchup = {}
        self.catchup_detail = None
        self.human_seen()

    def focus_changed(self, focused: bool) -> None:
        stamp = time.time()
        if focused and stamp - self._focus_at >= 1:
            self.store.kv_set('gui_focused_at', stamp)
            self._focus_at = stamp
        elif not focused and self._focused:
            self.store.kv_set('gui_focused_at', 0)
        if focused and not self._focused:
            since = self.store.kv_get("human_last_seen")
            if since and stamp - since >= 600:
                s = self.store
                self.catchup = catchup_items(s.q("SELECT * FROM events WHERE ts>? ORDER BY id DESC", since),
                    s.questions(None, limit=100000), s.q("SELECT * FROM runs WHERE ended>?", since),
                    s.memories(limit=100000, include_superseded=True), since)
                for rows in self.catchup.values():
                    for row in rows:
                        row["label"] = self.names.event_text(row["label"])
                self.catchup_cost = s.scalar("SELECT sum(cost) FROM runs WHERE ended>?", since, default=0.0)
        if focused and not self.catchup and stamp - self._seen_at >= 10:
            self.human_seen()
        self._focused = focused

    def start_team(self) -> None:
        if self.service_action and self.service_action.is_alive():
            return
        def start():
            from ..service import start_service
            try:
                start_service(self.cfg)
            except Exception as exc:
                self.service_error = str(exc)
        self.service_error = ""
        self.service_action = threading.Thread(target=start, daemon=True)
        self.service_action.start()

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
        waiting = {r["key"] for r in s.q("SELECT key FROM kv WHERE (key LIKE 'safety.waiting.%' OR key LIKE 'safety.baseline_wait.%') AND value='true'")}
        for task in self.tasks:
            task["awaiting_human"] = any(f"{prefix}.{task['id']}" in waiting for prefix in ("safety.waiting", "safety.baseline_wait"))
            task["merge_check"] = ("checking…" if task["id"] == checking else
                                   "checks failed" if f"check_failed.{task['id']}" in failed else "")
        self.questions = s.questions("open")
        self.events = s.events(limit=300)
        for event in self.events:
            event["text"] = self.names.event_text(event["text"])
        answer_event = s.max_event_id()
        if self._help_event is not None:
            self.new_help += s.q("SELECT * FROM events WHERE id>? AND id<=? AND kind='needs_help' ORDER BY id",
                                self._help_event, answer_event)
        self._help_event = answer_event
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
        self.memories = s.memories(limit=400, include_superseded=True)
        self.kv = {k: s.kv_get(k) for k in ("paused", "stopped", "heartbeat", "throttled", "claude_ratelimit",
                                                       "usage:codex", "limit.claude", "limit.codex", "limit.local",
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
        # run history for watched agents: a bounded, flat-size fetch every cycle (not the grown
        # "load older" depth) unless a load_older_runs() click queued a deeper page, merged into the
        # cache so it never evicts already-paged-in history — this is the only place runs are
        # fetched from the store, matching every other snapshot above; runs_for()/has_more_runs()/
        # load_older_runs() are all pure cache reads or queue writes, safe to call from draw code.
        for run_id, agent_id in self._run_targets.items():
            row = s.one("SELECT * FROM runs WHERE id=? AND agent=?", run_id, agent_id)
            if row:
                self._runs_cache.setdefault(agent_id, {})[run_id] = row
        self._run_targets.clear()
        for agent_id in self._runs_watched:
            pending = self._runs_older_pending.pop(agent_id, None)
            if pending is not None:
                self._fetch_and_merge_runs(agent_id, pending)
                self._runs_older_limit[agent_id] = pending
            else:
                self._fetch_and_merge_runs(agent_id, RUNS_PAGE)

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

    def _fetch_and_merge_runs(self, agent_id: str, limit: int) -> None:
        """Fetch the `limit` most recent runs and merge them into the cache by id — never replaces
        or trims the cache wholesale, so a fetch at one depth can't evict rows a deeper fetch (an
        earlier load_older_runs call) already proved exist. Over-fetches by one to tell "exactly
        `limit` runs total" apart from "more than `limit` exist" without a separate COUNT query.
        Exhaustion only ever tightens (False -> True): once a deep enough fetch has proven there's
        no older run left, a later shallower one (the flat-size periodic refresh) must not un-prove
        it just because it wasn't asked to look that far back."""
        rows = self.store.runs(agent_id, limit=limit + 1)
        exhausted = len(rows) <= limit
        self._runs_exhausted[agent_id] = exhausted or self._runs_exhausted.get(agent_id, False)
        cache = self._runs_cache.setdefault(agent_id, {})
        for row in rows[:limit]:  # the +1 was only to detect exhaustion; don't cache past the asked-for depth
            cache[row["id"]] = row

    def request_run(self, agent_id: str, run_id: int) -> None:
        """Queue an exact navigation target; refresh owns all database reads."""
        self._runs_watched.add(agent_id)
        self._run_targets[run_id] = agent_id

    def runs_for(self, agent_id: str) -> list[dict]:
        """Cached run history for an agent, newest first — a pure cache read with no store access,
        safe to call every draw frame. Marks the agent as "watched" so refresh() (called once per
        frame, outside the draw path) starts keeping its recent runs current; historical pages
        loaded via load_older_runs() persist across those refreshes so no run — however old — is
        ever permanently out of reach (see REQ-GUI-022). Returns [] for at most one refresh cycle
        the first time an agent is viewed, before refresh() has had a chance to populate it."""
        self._runs_watched.add(agent_id)
        cache = self._runs_cache.get(agent_id, {})
        return sorted(cache.values(), key=lambda run: -run["id"])

    def has_more_runs(self, agent_id: str) -> bool:
        self._runs_watched.add(agent_id)
        return not self._runs_exhausted.get(agent_id, False)

    def load_older_runs(self, agent_id: str) -> None:
        """Queue another page of older history for an agent. Unlike most user-triggered Data actions
        (send_chat, answer, ...), this doesn't fetch synchronously — the "Load older runs" click
        happens from inside draw code, and a page can be hundreds of rows of prompt/system/summary
        text, so the fetch itself must happen in refresh() on the next cycle, outside the draw path.
        Safe to call more than once before that cycle runs: each call advances the queued depth by
        one more page rather than clobbering a still-pending request."""
        self._runs_watched.add(agent_id)
        requested = max(self._runs_older_limit.get(agent_id, RUNS_PAGE), self._runs_older_pending.get(agent_id, 0))
        self._runs_older_pending[agent_id] = requested + RUNS_PAGE

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

    def stop_now(self) -> None:
        from ..safety import stop_now
        stop_now(self.store)
        self.refresh(force=True)

    def resume(self) -> None:
        from ..safety import resume
        resume(self.store)

    def set_paused(self, paused: bool) -> None:
        if self.kv.get("stopped"):
            if not paused:
                self.resume()
            return
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

    def set_memory(self, memory_id: int, **fields) -> None:
        self.store.update_memory(memory_id, actor="human", **fields)
        self.refresh(force=True)

    def delete_memory(self, memory_id: int) -> None:
        self.store.delete_memory(memory_id, actor="human")
        self.refresh(force=True)



def catchup_items(events: list[dict], questions: list[dict], runs: list[dict], memories: list[dict], since: float) -> dict:
    """Select notable activity without depending on a window or mutable GUI state."""
    groups = {key: [] for key in ('Merges', 'Rejected tasks', 'Failed checks', 'Blocked tasks', 'Failed runs', 'Decisions', 'Questions')}
    for event in events:
        if event['ts'] <= since:
            continue
        text = event['text'].lower()
        group = ('Failed checks' if 'checks failed' in text else 'Rejected tasks' if 'rejected #' in text else
                 'Blocked tasks' if 'blocked' in text and event['kind'] == 'task' else
                 'Merges' if 'merged #' in text else None)
        if group:
            groups[group].append(dict(label=event['text'], ref=event['ref'], body=event['text']))
    for run in runs:
        if (run.get('ended') or 0) > since and run['status'] in ('failed', 'error'):
            groups['Failed runs'].append(dict(label=f"{run['agent']} · run #{run['id']}", ref=f"run:{run['id']}", body=run['summary'], agent=run['agent']))
    for memory in memories:
        if memory['ts'] > since and memory['kind'] == 'decision':
            groups['Decisions'].append(dict(label=memory['title'], ref=f"mem:{memory['id']}", body=memory['content']+'\n\n'+memory['rationale']))
    for question in sorted(questions, key=lambda q: (q['status'] != 'open', -q['id'])):
        if question['ts'] > since:
            groups['Questions'].append(dict(label=question['question'], ref=f"q:{question['id']}", body=question['context']+'\n\n'+(question['answer'] or ''), agent=question['asker']))
    return {key: rows for key, rows in groups.items() if rows}
