"""The orchestrator engine: decides who wakes up, why, and with what context — then runs them."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import threading
import time
import traceback
from dataclasses import dataclass
from collections import deque
from datetime import datetime
from pathlib import Path

from . import gitops, config as config_mod
from .config import AgentCfg, Config
from .roles import CHARTER, get_role
from .triage import MailTriage, fyi_message, mandatory, changed_without_pending_mail
from .gates import MergeGateMixin
from .safety import audit, fingerprint, kill_group
from .runners import RunSpec, make_runner, Runner, usage_limit, reported_reset
from .store import OPEN_STATUSES, Store, now, HandleBook
from .team import ago, fmt_message, fmt_task_full, fmt_task_line

TICK = 1.0
MESSAGE_DEBOUNCE = 2.0  # let bursts of mail land before waking someone
CHAT_DEBOUNCE = 0.4
ESCALATION_TIMEOUT_MINUTES = 30  # REQ-COM-029: auto-forward an unhandled escalation after this long
ESCALATION_URGENT_TIMEOUT_MINUTES = 5  # ...for urgency="urgent"


@dataclass
class Wake:
    priority: int
    agent: AgentCfg
    reason: str  # chat | review | task | messages | proactive | poke
    task: dict | None = None

    @property
    def chat(self) -> bool:
        return self.reason == "chat"


REASONS = {
    "chat": "the human is chatting with you",
    "review": "a task is waiting for your review",
    "task": "you have a task to work on",
    "messages": "you have new mail",
    "proactive": "proactive check-in (things changed since you last looked)",
    "poke": "the human asked you to check in now",
}


class Engine(MergeGateMixin):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.running: dict[str, tuple[Runner, asyncio.Task, Wake]] = {}
        self.failures: dict[str, tuple[int, float]] = {}  # agent -> (count, retry_after)
        self._run_started: dict[str, float] = {}  # agent -> run start time (REQ-ENG-050)
        self._last_output: dict[str, float] = {}  # agent -> time of last stream event (REQ-ENG-050)
        self._run_worktree: dict[str, bool] = {}  # agent -> run's cwd is a task worktree (REQ-ENG-050)
        self._watchdog_reason: dict[str, str] = {}  # agent -> "stalled" | "timeout" (REQ-ENG-050)
        self._watchdog_killed_at: dict[str, float] = {}  # agent -> when the kill signal was sent
        self.pokes: set[str] = set()
        self.mail_triage = MailTriage()
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self._merge_task: asyncio.Task | None = None
        self._merge_lock = threading.Lock()
        self._config_stamps = self.config_stamps()
        self._session_versions: dict[str, int] = {}
        self.save_config_snapshot()

    def save_config_snapshot(self) -> None:
        self.store.kv_set("config.last_good", {"toml": self.cfg.toml_data, "team": self.cfg.team_data})

    def config_stamps(self) -> dict[str, tuple[int, int] | None]:
        stamps = {}
        for name in (config_mod.TEAM_FILE, config_mod.CONFIG_FILE):
            try:
                stat = (self.cfg.state_dir / name).stat()
                stamps[name] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                stamps[name] = None
        return stamps

    def reload_config(self) -> None:
        pending = self.store.kv_get("safety.config")
        if pending and pending["payload"] != self.store.kv_get("safety.approved"):
            from .gates import verdict
            if verdict(self.store, pending) == "approve":
                self._config_stamps.pop(config_mod.CONFIG_FILE, None)
        for name, stamp in self.config_stamps().items():
            if stamp == self._config_stamps.get(name):
                continue
            self._config_stamps[name] = stamp
            try:
                team = config_mod.read_team(self.cfg.root) if name == config_mod.TEAM_FILE else self.cfg.team_data
                raw = config_mod.read_toml(self.cfg.root) if name == config_mod.CONFIG_FILE else self.cfg.toml_data
                updated = config_mod.load(self.cfg.root, team_data=team, toml_data=raw)
            except (ValueError, TypeError, KeyError) as e:
                message = f"{name} invalid: {e}"
                self.store.kv_set(f"config_error.{name}", message)
                self.store.event("system", "error", message, significant=False)
                lead = next(a.id for a in self.cfg.agents if a.role == "lead")
                self.store.send("system", lead, message, subject=f"{name} invalid", kind="system")
                continue
            self.sync_config_agents(updated)
            self.cfg = updated
            self.save_config_snapshot()
            self.store.kv_set(f"config_error.{name}", "")
            self.store.event("system", "config", f"Reloaded {name}", significant=False)

    def sync_config_agents(self, updated: Config) -> None:
        for old in self.store.agents():
            aid = old["id"]
            new = updated.agent(aid)
            if not new or old["role"] != new.role:
                tasks = self.store.q("SELECT * FROM tasks WHERE status NOT IN ('done','cancelled') "
                                     "AND (assignee=? OR reviewer=?)", aid, aid)
                for task in tasks:
                    self.store.update_task(task["id"], actor="system", status="ready", assignee=None,
                                           reviewer=None, next_attempt_at=0,
                                           event_text=f"#{task['id']} returned to ready after {aid} changed")
            if not new or old["backend"] != new.backend:
                self._session_versions[aid] = self._session_versions.get(aid, 0) + 1
                self.store.set_agent(aid, session_id=None, session_runs=0)
                self.store.kv_set(f"local_history:{aid}", [])
        self.store.sync_agents(updated.agents)
        for a in updated.agents:
            self.store.set_agent(a.id, enabled=int(a.enabled))

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start_thread(self) -> threading.Thread:
        self.thread = threading.Thread(target=lambda: asyncio.run(self.main()), name="troupe-engine", daemon=True)
        self.thread.start()
        return self.thread

    def stop(self) -> None:
        self._stop.set()

    def recover(self) -> None:
        s = self.store
        self.sync_config_agents(self.cfg)
        for name in (config_mod.TEAM_FILE, config_mod.CONFIG_FILE):
            s.kv_set(f"config_error.{name}", "")
        self._recover_interrupted_runs()
        for a in self.cfg.agents:
            s.set_agent(a.id, enabled=int(a.enabled))
        s.kv_set("checking_task", None)
        self.cleanup_worktrees()

    def _recover_interrupted_runs(self) -> None:
        """#121: like service.recover_interrupted, but keyed on run id, not a blind status sweep — a
        run still marked 'running' after a crash or restart (today's actual driver: a reinstall
        racing a run) may have already landed its real side effects (e.g. the builder's own
        complete_task call) before the process died, well before that run's own end_run() got a
        chance to record it. Redelivering its mail regardless wastes a run the agent can only reply
        "already handled this" to — see _requeue_or_deliver_read."""
        s = self.store
        for run in s.q("SELECT id FROM runs WHERE status='running'"):
            self._requeue_or_deliver_read(s.kv_get(f"run_mail.{run['id']}", []))
            s.x("DELETE FROM kv WHERE key=?", f"run_mail.{run['id']}")
        s.x("UPDATE runs SET status='interrupted', ended=? WHERE status='running'", now())
        s.x("UPDATE agents SET state='idle',current_run=NULL,activity='' WHERE state='running'")

    def _requeue_or_deliver_read(self, message_ids: list[int]) -> None:
        """#121: mail belonging to a run that produced no usable reply (limited, watchdog-killed,
        failed, cancelled, or orphaned by a crash/restart) still needs to reach the recipient again
        — redelivered by default, exactly like the pre-#121 behavior. Marked read instead only on
        positive evidence that *this specific recipient* already acted on the mail's own task after
        it arrived: update_task (complete_task, review_task, and update_task itself all go through
        it) logs a `task:<id>` event with `agent=actor` — if the recipient authored one for this
        message's task_id after the message's own ts, they've already engaged with it themselves.

        QA #121 round 1 (rejected): the task's *current* status alone proves nothing about whether
        this recipient read this particular message — a reviewer is mailed about tasks sitting in
        "review", a builder about "ready" ones, a lead about any status — so treating any status
        other than in_progress/blocked as "handled" silently dropped real mail (a rate-limited or
        failed run for exactly those roles). Only a same-recipient, same-task, later-than-the-mail
        event counts."""
        if not message_ids:
            return
        s = self.store
        rows = s.q(f"SELECT id, task_id, recipient, ts FROM messages WHERE id IN ({','.join('?' * len(message_ids))})",
                  *message_ids)
        done_ids, retry_ids = [], []
        for row in rows:
            handled = bool(row["task_id"]) and s.scalar(
                "SELECT 1 FROM events WHERE ref=? AND agent=? AND ts>? LIMIT 1",
                f"task:{row['task_id']}", row["recipient"], row["ts"], default=None)
            (done_ids if handled else retry_ids).append(row["id"])
        s.mark_read(done_ids)
        s.mark_unread(retry_ids)

    def cleanup_worktrees(self) -> None:
        try:
            for task_id, path in gitops.task_worktrees(self.cfg.root, self.cfg.worktrees_dir):
                task = self.store.task(task_id)
                if task and task["status"] not in ("done", "cancelled"):
                    continue
                # An open task may point to a tree with a different legacy name/id.
                if self.store.scalar("SELECT 1 FROM tasks WHERE worktree=? AND status NOT IN ('done','cancelled')",
                                     str(path)):
                    continue
                try:
                    gitops.remove_worktree(self.cfg.root, path)
                except gitops.GitError as e:
                    self.store.event("system", "error", str(e), significant=False)
            gitops.prune_worktrees(self.cfg.root)
        except gitops.GitError as e:
            self.store.event("system", "error", f"Worktree cleanup failed: {e}", significant=False)

    async def main(self) -> None:
        from .api import APIServer
        from .notify import Notifier

        from .usage import monitor

        usage_task = None
        self.api = APIServer(self.cfg, self)
        notifier_task = None
        try:
            self.api.start()
            usage_task = asyncio.create_task(monitor(self))
            notifier_task = asyncio.create_task(Notifier(self.store).run(self))
            await self._serve()
        finally:
            if usage_task:
                usage_task.cancel()
                await asyncio.gather(usage_task, return_exceptions=True)
            if notifier_task:
                notifier_task.cancel()
                await asyncio.gather(notifier_task, return_exceptions=True)
            self.api.stop()

    async def _serve(self) -> None:
        self.recover()
        self.store.event("system", "engine", "Engine started", significant=False)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                self.store.event("system", "error", "engine tick failed: " + traceback.format_exc()[-600:],
                                 significant=False)
            await asyncio.sleep(TICK)
        runs = list(self.running.values())
        groups = {id(runner): runner.proc.pid for runner, _, _ in runs
                  if runner.proc and runner.proc.returncode is None}
        for runner, _task, _w in runs:
            runner.kill()
        if runs:
            await asyncio.sleep(0.5)
            for runner, task, _w in runs:
                if runner.proc and runner.proc.returncode is None:
                    groups[id(runner)] = runner.proc.pid
                if id(runner) in groups:
                    try:
                        os.killpg(groups[id(runner)], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await runner.proc.wait()
                if not task.done():
                    task.cancel()
            await asyncio.gather(*(task for _, task, _ in runs), return_exceptions=True)
        self.store.x("UPDATE runs SET status='interrupted', ended=? WHERE status='running'", now())
        self.store.x("UPDATE agents SET state='idle',current_run=NULL,activity='' WHERE state='running'")
        if self._merge_task is not None:
            await self._merge_task

    # ── the loop ──────────────────────────────────────────────────────────
    async def tick(self) -> None:
        s = self.store
        s.kv_set("heartbeat", now())
        self.reload_config()
        self.handle_commands()
        if self._stop.is_set():
            return
        if s.kv_get("stopped"):
            for runner, running_task, _ in list(self.running.values()):
                runner.kill()
                if runner.proc is None:
                    running_task.cancel()
            return
        self.check_claude_cap()
        if self._merge_task is not None and self._merge_task.done():
            try:
                self._merge_task.result()
            except Exception as e:
                s.event("system", "error", f"Merge worker failed: {e}", significant=False)
            self._merge_task = None
        if self._merge_task is None:
            self._merge_task = asyncio.create_task(asyncio.to_thread(self.process_approved))
        self.watchdog_sweep()
        self.escalation_sweep()
        self.sweep_zombie_runs()
        paused = bool(s.kv_get("paused", False))
        if not paused:
            self.dispatch()
        wakes = sorted(self.candidates(paused), key=lambda w: w.priority)
        def task_work(w: Wake) -> bool:
            return bool(not w.chat and w.task and get_role(w.agent.role).works_in_task_tree
                        and w.task["status"] in ("ready", "in_progress")
                        and self.deps_done(w.task) and now() >= (w.task["next_attempt_at"] or 0))

        # Give task work the next free autonomous slot when none is running.
        work = [w for w in wakes if task_work(w)]
        if work and not any(task_work(rw) for _, _, rw in self.running.values()):
            chosen = min(work, key=lambda w: (s.agent(w.agent.id)["last_run_at"] or 0, w.agent.id))
            wakes.remove(chosen)
            wakes.insert(next((i for i, w in enumerate(wakes) if not w.chat), len(wakes)), chosen)
        for w in wakes:
            if w.agent.id in self.running:
                continue
            n_all = len(self.running)
            n_auto = sum(1 for _, _, rw in self.running.values() if not rw.chat)
            if w.chat:
                if n_all >= self.cfg.budget.max_concurrent + 2:
                    continue
            elif n_auto >= self.cfg.budget.max_concurrent or not self.budget_ok():
                continue
            await self.launch(w)
        self.publish_wait_states(wakes)

    def publish_wait_states(self, wakes: list[Wake] | None = None) -> dict[str, dict]:
        """Publish scheduler-owned wait reasons; clients consume the stored snapshot."""
        s, stamp = self.store, now()
        names = HandleBook(self.cfg.project, self.cfg.agents)
        tasks = s.tasks(limit=1000000)
        task_by_id = {t['id']: t for t in tasks}
        questions = s.questions(limit=1000000)
        agents = {a['id']: a for a in s.agents()}
        previous = s.wait_states()
        queued = {r['recipient']: r['n'] for r in s.q('SELECT recipient,count(*) n FROM messages WHERE read_at IS NULL GROUP BY recipient')}
        pending = [w for w in (wakes or []) if w.agent.id not in self.running]
        slots = {}
        n_auto = sum(not w.chat for _, _, w in self.running.values())
        for wake in pending:
            full = len(self.running) >= self.cfg.budget.max_concurrent + 2 if wake.chat else n_auto >= self.cfg.budget.max_concurrent
            if full:
                slots[wake.agent.id] = len(slots) + 1
        states = {}
        for cfg in self.cfg.agents:
            row = agents.get(cfg.id)
            if not row:
                continue
            waiting = None
            mine = [t for t in tasks if t['assignee'] == cfg.id and t['status'] not in ('done', 'cancelled')]
            if row['state'] != 'running':
                human = [q for q in questions if q['asker'] == cfg.id or
                         (q['kind'] in ('approval', 'safety') and any(t['id'] == q['task_id'] for t in mine))]
                reviews = [t for t in mine if t['status'] == 'review']
                dependencies = sorted({dep for t in mine for dep in t['depends_on']
                                       if dep in task_by_id and task_by_id[dep]['status'] not in ('done', 'cancelled')})
                blocked = [t for t in mine if t['status'] == 'blocked']
                providers = list(dict.fromkeys(p.provider for p in cfg.providers)) or [cfg.backend]
                limits = {p: s.kv_get(f'limit.{p}', 0) or 0 for p in providers}
                if human:
                    waiting = dict(kind='human', targets=['human'], detail='Awaiting questions ' + ', '.join(f"#{q['id']}" for q in human))
                elif reviews:
                    reviewers = sorted({names.name(t['reviewer']) for t in reviews if t['reviewer']})
                    if not reviewers:
                        reviewers = [names.name(a.id) for a in self.cfg.agents if a.role == 'qa' and a.enabled]
                    waiting = dict(kind='review', targets=reviewers, detail='Review of ' + ', '.join(f"#{t['id']}" for t in reviews))
                elif dependencies:
                    waiting = dict(kind='dependency', targets=dependencies, detail='Waiting for prerequisite tasks')
                elif blocked:
                    t = blocked[0]
                    notes = s.task_notes(t['id'])
                    reason = notes[-1]['text'] if notes else t['review_notes'] or t['description'] or 'Task is blocked'
                    waiting = dict(kind='blocked', targets=[t['id']], detail=reason)
                elif limits and all(reset > stamp for reset in limits.values()):
                    waiting = dict(kind='providers', targets=providers, detail='All configured providers are limited', reset_at=min(limits.values()))
                elif (s.kv_get(f'limit.{row["backend"]}', 0) or 0) > stamp:
                    waiting = dict(kind='rate_limit', targets=[row['backend']], detail='Current provider is limited', reset_at=s.kv_get(f'limit.{row["backend"]}'))
                elif cfg.id in slots:
                    waiting = dict(kind='slot', targets=[], detail='Waiting for a run slot', queue_position=slots[cfg.id])
                elif row['enabled'] and any(t['status'] in ('ready', 'in_progress') for t in mine) and stamp-(row['last_run_at'] or 0)>180:
                    waiting = dict(kind='parked', targets=[], detail='Idle while owing work')
                if waiting:
                    old = previous.get(cfg.id, {}).get('waiting_on')
                    waiting['since'] = old['since'] if old and old['kind'] == waiting['kind'] else stamp
            states[cfg.id] = dict(waiting_on=waiting, mail_queued=queued.get(cfg.id, 0),
                mail_reading=len(s.kv_get(f'run_mail.{row["current_run"]}', [])) if row['state'] == 'running' else 0)
        s.publish_wait_states(states)
        return states

    def budget_ok(self) -> bool:
        b, s = self.cfg.budget, self.store
        hour = s.scalar("SELECT COUNT(*) FROM runs WHERE started>? AND chat=0", now() - 3600, default=0)
        if b.max_runs_per_hour and hour >= b.max_runs_per_hour:
            s.kv_set("throttled", f"run limit ({b.max_runs_per_hour}/h) reached")
            return False
        if b.max_usd_per_day:
            spent = s.scalar("SELECT SUM(cost) FROM runs WHERE started>?", now() - 86400, default=0.0)
            if spent >= b.max_usd_per_day:
                s.kv_set("throttled", f"daily budget ${b.max_usd_per_day:.2f} reached")
                return False
        s.kv_set("throttled", "")
        return True

    def handle_commands(self) -> None:
        s = self.store
        for c in s.pending_commands():
            cmd, arg = c["cmd"], c["arg"]
            if cmd == "stop_now":
                if not s.kv_get("stopped"):
                    audit(s, "Stop everything requested by the human")
                s.kv_set("stopped", True)
                s.kv_set("paused", True)
                for runner, _, _ in list(self.running.values()):
                    runner.kill()
            elif cmd == "resume":
                s.kv_set("stopped", False)
                s.kv_set("paused", False)
                audit(s, "Human resumed the troupe", notify=False)
            elif cmd == "stop_team":
                self.stop()
            elif cmd == "stop" and arg in self.running:
                self.running[arg][0].kill()
                s.event("human", "control", f"You stopped {arg}'s run", significant=False)
            elif cmd == "poke":
                self.pokes.add(arg)
                self.failures.pop(arg, None)
            elif cmd == "reset_session":
                s.set_agent(arg, session_id=None, session_runs=0)
                s.kv_set(f"local_history:{arg}", [])
                s.event("human", "control", f"You reset {arg}'s session", significant=False)
            elif cmd == "enable":
                s.set_agent(arg, enabled=1)
            elif cmd == "disable":
                s.set_agent(arg, enabled=0)
                if arg in self.running:
                    self.running[arg][0].kill()

    # ── task flow ─────────────────────────────────────────────────────────
    def deps_done(self, t: dict) -> bool:
        for d in t["depends_on"]:
            dt = self.store.task(int(d))
            if dt and dt["status"] not in ("done", "cancelled"):
                return False
        return True

    def dispatch(self) -> None:
        """Assign unowned ready tasks to the least-loaded enabled agent of the right role."""
        s = self.store
        open_tasks = s.tasks(OPEN_STATUSES)
        enabled = {a["id"] for a in s.agents() if a["enabled"]}
        for t in open_tasks:
            if t["status"] != "ready" or t["assignee"] or not self.deps_done(t):
                continue
            pool = [a for a in self.cfg.agents_with_role(t["role"]) if a.id in enabled]
            if not pool:
                continue

            def load(a: AgentCfg) -> int:
                return sum(1 for x in open_tasks if x["assignee"] == a.id and x["status"] in ("ready", "in_progress", "blocked"))

            best = min(pool, key=lambda a: (load(a), a.id in self.running))
            if load(best) > 0 and get_role(t["role"]).works_in_task_tree:
                continue  # builders get one task at a time; wait for a free one
            s.update_task(t["id"], actor="system", assignee=best.id,
                          event_text=f"Dispatched #{t['id']} {t['title']} → {best.id}", significant=False)
            t["assignee"] = best.id

    def current_task(self, a: AgentCfg) -> dict | None:
        """The task an agent should be working on right now (and whose worktree it lives in)."""
        mine = [t for t in self.store.tasks(OPEN_STATUSES) if t["assignee"] == a.id]
        for status in ("in_progress", "blocked"):
            for t in mine:
                if t["status"] == status:
                    return t
        for t in mine:
            if t["status"] == "ready" and self.deps_done(t):
                return t
        return None

    def start_task(self, a: AgentCfg, t: dict) -> dict:
        role = get_role(a.role)
        fields: dict = {"status": "in_progress"}
        if role.works_in_task_tree and not t["worktree"]:
            try:
                existed = (self.cfg.worktrees_dir / f"t{t['id']}").exists()
                branch, path = gitops.create_worktree(self.cfg.root, self.cfg.worktrees_dir, t["id"], t["title"])
                fields.update(branch=branch, worktree=str(path))
                if not existed and self.cfg.git.setup:
                    self.store.kv_set(f"setup.{path}", {"status": "pending", "command": self.cfg.git.setup})
            except gitops.GitError as e:
                self.store.event("system", "error", f"worktree for #{t['id']} failed: {e}", significant=False)
        # #121: actor="system", not a.id -- this is the engine's own dispatch-adjacent bookkeeping
        # (same convention as dispatch()'s own update_task call below), not something the agent
        # itself did. _requeue_or_deliver_read treats a task event authored by a message's own
        # recipient as evidence they've acted on it; crediting the agent here would make every
        # task start look like already-handled mail, dropping the message that started it.
        self.store.update_task(t["id"], actor="system", event_text=f"{a.id} started #{t['id']} {t['title']}", **fields)
        return self.store.task(t["id"]) or t

    def backend_limited(self, backend: str) -> bool:
        return (self.store.kv_get(f"limit.{backend}", 0) or 0) > now()

    def limit_reason(self, backend: str) -> str:
        return (self.store.kv_get(f"limit_meta.{backend}", {}) or {}).get("reason", "provider")

    def record_limit(self, backend: str, until: float, reported: bool = True, reason: str = "provider") -> None:
        key = f"limit.{backend}"
        previous = self.store.kv_get(key, 0) or 0
        meta = self.store.kv_get(f"limit_meta.{backend}", {})
        # Older timestamps lack provenance; preserve them as reported limits.
        was_reported = meta.get("reported", True) if meta.get("until") == previous else True
        if previous > now():
            if was_reported and not reported:
                return
            if was_reported == reported:
                until = max(previous, until)
        self.store.kv_set(key, until)
        self.store.kv_set(f"limit_meta.{backend}", {"until": until, "reported": reported, "reason": reason})

    def check_claude_cap(self) -> None:
        """REQ-BE-016 (#72): an MVP usage cap. Each claude usage window (5h, 7d — same
        claude_ratelimit data the usage meters read) has its own cap, `[budget]
        claude_cap_5h_percent`/`claude_cap_7d_percent` (each falling back to claude_cap_percent
        when unset; 0 = off). If a window's latest known utilization is at or above its own cap,
        block new autonomous claude runs via the existing per-backend limit mechanism (reason
        "cap") until *that window's* reset. Chat is exempt (see launch()) — the human is present
        and can decide — but the header shows it as over the cap.

        Once triggered, this doesn't re-check until the limit clears, and even then won't re-cap
        off the *same* stale snapshot: nothing refreshes claude_ratelimit while claude is capped
        (no runs happen to report fresh usage), so re-triggering on stale data would cap forever.
        Letting one tick through on stale data gives a real run a chance to report a fresh reading.
        """
        if self.backend_limited("claude"):
            return
        snapshot = self.store.kv_get("claude_ratelimit") or {}
        snapshot_at = snapshot.get("at")
        if snapshot_at is not None and snapshot_at == self.store.kv_get("claude_cap_snapshot_at"):
            return
        budget = self.cfg.budget
        tripped = None  # (pct, cap, until, window_key) of the window that tripped, if any
        for key, window in (snapshot.get("unifiedWindows") or {}).items():
            cap = config_mod.claude_window_cap(budget, key)
            if not cap:
                continue
            pct = float(window.get("utilization") or 0) * 100
            if pct < cap:
                continue
            reset = window.get("resetsAt") or window.get("resets_at") or window.get("reset")
            if isinstance(reset, str):
                try:
                    reset = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    reset = None
            if tripped is None or pct > tripped[0]:
                tripped = (pct, cap, reset, key)
        if tripped is None:
            return
        pct, cap, reset, key = tripped
        until = reset if reset and reset > now() else now() + 900
        self.record_limit("claude", until, reported=True, reason="cap")
        self.store.kv_set("claude_cap_snapshot_at", snapshot_at)
        label = {"five_hour": "5h", "seven_day": "7d"}.get(key, key)
        self.store.event("system", "providers",
                         f"Claude usage cap engaged: {label} at {pct:.0f}% >= {cap:.0f}% — new autonomous claude "
                         f"runs paused until {time.strftime('%H:%M', time.localtime(until))} (chat still works)")

    def escalation_sweep(self) -> None:
        """REQ-COM-029: an escalation the PM hasn't triaged (forward_to_human/answer_escalation/
        batch_to_human) within the timeout auto-forwards to the human anyway — a busy or down PM
        can't bury it. Urgent escalations get a shorter timeout."""
        pm = next((a for a in self.cfg.agents if a.role == "pm"), None)
        pm_id = pm.id if pm else "pm"
        t = now()
        for esc in self.store.escalations(status="open"):
            timeout = ESCALATION_URGENT_TIMEOUT_MINUTES if esc["urgency"] == "urgent" else ESCALATION_TIMEOUT_MINUTES
            if t - esc["ts"] >= timeout * 60:
                self.store.auto_forward_escalation(esc["id"], pm_id)

    # ── who should wake ───────────────────────────────────────────────────
    def candidates(self, paused: bool) -> list[Wake]:
        s = self.store
        if s.kv_get("stopped"):
            return []
        t_now = now()
        out: list[Wake] = []
        db_agents = {a["id"]: a for a in s.agents()}
        review_queue = [t for t in s.tasks(("review",))]
        for a in self.cfg.agents:
            row = db_agents.get(a.id)
            if a.id in self.running or not row or not row["enabled"]:
                continue
            # A "cap" limit (REQ-BE-016) blocks autonomous wakes but not chat — the human is
            # present and can decide. A real provider limit blocks everything, chat included.
            limited = self.backend_limited(a.backend)
            if limited and self.limit_reason(a.backend) != "cap":
                continue
            fails, retry_after = self.failures.get(a.id, (0, 0))
            if t_now < retry_after:
                continue
            unread = s.unread(a.id)
            chat = [m for m in unread if m["sender"] == "human"]
            if chat:
                if t_now - chat[-1]["ts"] >= CHAT_DEBOUNCE:
                    out.append(Wake(0, a, "chat", self.current_task(a)))
                continue  # the human is typing to this agent: nothing else preempts that
            if limited:
                continue  # capped, and nothing for chat to preempt — no autonomous wake
            if paused:
                continue
            if a.id in self.pokes:
                out.append(Wake(1, a, "poke", self.current_task(a)))
                continue
            if a.role == "qa":
                mine = [t for t in review_queue if t["reviewer"] in (None, a.id)
                        and not s.kv_get(f"safety.waiting.{t['id']}")]
                if mine:
                    out.append(Wake(1, a, "review", mine[0]))
                    continue
            real_mail = [m for m in unread if m["kind"] != "system" or m["sender"] != "system" or mandatory(m, a, s)]
            if (unread and t_now - unread[-1]["ts"] >= MESSAGE_DEBOUNCE and real_mail
                    and self.mail_triage.should_wake(self.cfg, s, a, real_mail, self.current_task(a))):
                out.append(Wake(2, a, "messages", self.current_task(a)))
                continue
            task = self.current_task(a)
            if task and task["status"] in ("ready", "in_progress") and t_now >= (task["next_attempt_at"] or 0):
                out.append(Wake(3, a, "task", task))
                continue
            if (a.idle_seconds > 0 and t_now - (row["last_run_at"] or 0) >= a.idle_seconds
                    and changed_without_pending_mail(s, a.id, row["last_event_seen"] or 0, unread)):
                out.append(Wake(5, a, "proactive", task))
        return out

    # ── running an agent ──────────────────────────────────────────────────
    async def launch(self, w: Wake) -> None:
        a, s = w.agent, self.store
        if s.kv_get("stopped"):
            return
        if self.backend_limited(a.backend) and not (w.chat and self.limit_reason(a.backend) == "cap"):
            return
        self.pokes.discard(a.id)
        task = w.task
        role = get_role(a.role)
        if task and task["status"] == "ready" and (w.reason == "task" or role.works_in_task_tree):
            task = self.start_task(a, task)
        if w.reason == "review" and task:
            s.update_task(task["id"], actor=a.id, reviewer=a.id,
                          event_text=f"{a.id} is reviewing #{task['id']}", significant=False)
            task = s.task(task["id"])
        cwd = self.cfg.root
        if role.works_in_task_tree and task and task.get("worktree") and Path(task["worktree"]).exists():
            cwd = Path(task["worktree"])
        msgs = s.unread(a.id)
        self.mail_triage.forget(a.id)
        s.mark_read([m["id"] for m in msgs])
        if any(m["sender"] == "human" and m["kind"] == "chat" for m in msgs):
            w.reason = "chat"  # whatever woke them, the human gets a live reply
        row = s.agent(a.id) or {}
        seen = s.max_event_id()
        prompt = self.build_prompt(a, w, msgs, task, row)
        system = self.system_prompt(a)
        system_hash = fingerprint(system)
        if s.kv_get(f"session_prompt.{a.id}") != system_hash:
            row["session_id"] = None
            s.set_agent(a.id, session_id=None, session_runs=0)
            s.kv_set(f"local_history:{a.id}", [])
        s.kv_set(f"session_prompt.{a.id}", system_hash)
        run_id = s.start_run(a.id, w.reason, task["id"] if task else None, str(cwd), w.chat,
                             prompt=prompt, system=system)
        s.kv_set(f"run_mail.{run_id}", [m["id"] for m in msgs])
        s.mark_read([m["id"] for m in msgs])
        s.set_agent(a.id, state="running", current_run=run_id, activity=REASONS[w.reason], last_run_at=now(),
                    last_event_seen=seen)
        s.event(a.id, "run", f"{a.id} woke up: {REASONS[w.reason]}", ref=f"run:{run_id}", significant=False)
        runner = make_runner(a.backend)
        spec = RunSpec(cfg=self.cfg, agent=a, system=system, prompt=prompt, cwd=cwd,
                       session_id=row.get("session_id"), log_path=self.cfg.runs_dir / f"{run_id:06d}-{a.id}.jsonl")
        atask = asyncio.create_task(self._run(runner, spec, w, run_id, msgs, task, self._session_versions.get(a.id, 0)))
        self.running[a.id] = (runner, atask, w)
        self._run_started[a.id] = now()
        self._last_output[a.id] = now()
        self._run_worktree[a.id] = cwd != self.cfg.root

    async def setup_worktree(self, runner: Runner, spec: RunSpec, task: dict | None, run_id: int) -> None:
        if not task or spec.cwd == self.cfg.root or runner.cancelled:
            return
        key = f"setup.{spec.cwd}"
        setup = self.store.kv_get(key, {})
        if setup.get("status") == "pending":
            setup.update(status="started", error="Worktree setup was interrupted before it finished.")
            self.store.kv_set(key, setup)
            spec.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.store.set_agent(spec.agent.id, activity="Setting up worktree…")
            try:
                with spec.log_path.open("a") as log:
                    log.write(json.dumps({"setup_command": setup["command"]}) + "\n")
                    runner.proc = await asyncio.create_subprocess_shell(
                        setup["command"], cwd=spec.cwd, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                    if runner.cancelled:
                        runner.kill()
                    assert runner.proc.stdout
                    tail = ""
                    while chunk := await runner.proc.stdout.read(65536):
                        text = chunk.decode(errors="replace")
                        log.write(json.dumps({"setup_output": text}) + "\n")
                        log.flush()
                        tail = (tail + text)[-2000:]
                    code = await runner.proc.wait()
                    log.write(json.dumps({"setup_exit": code}) + "\n")
                    setup["error"] = f"Worktree setup failed (exit {code}):\n{tail}" if code else ""
            except OSError as e:
                setup["error"] = f"Worktree setup failed: {e}"
            setup["status"] = "finished"
            self.store.kv_set(key, setup)
            if setup["error"]:
                self.store.task_note(task["id"], "system", setup["error"])
                self.store.run_line(run_id, "error", setup["error"])
        if setup.get("error"):
            footer = "\n\nPrinciple 0 applies: the human comes first."
            spec.prompt = (spec.prompt.removesuffix(footer) + "\n\n## Worktree setup\n" + setup["error"]
                           + "\nContinue the task; fix setup if needed." + footer)

    def watchdog_sweep(self) -> None:
        """REQ-ENG-050: kill runs that have gone silent past stall_minutes, or past their hard cap
        (max_run_minutes for worktree roles, max_coord_run_minutes otherwise — chat is exempt from
        the cap but not the stall rule). This only signals the process; `_run`'s own completion
        handling (unblocked once the process exits) does the mail requeue, backoff and notification,
        keyed off `self._watchdog_reason`."""
        from .service import kill_descendants

        t, b = now(), self.cfg.budget
        for a_id, (runner, atask, w) in list(self.running.items()):
            if runner is None:
                continue  # not a real run (tests reserve slots this way)
            if a_id in self._watchdog_reason:
                self._unstick_killed_run(a_id, runner, t)
                continue  # already killed; waiting for _run's completion handling to finish it up
            if runner.cancelled:
                continue
            proc = runner.proc
            if proc is None or proc.returncode is not None:
                continue  # not spawned yet, or already exited — _run will finish it up
            started = self._run_started.get(a_id, t)
            last_output = self._last_output.get(a_id, started)
            stalled = t - last_output > b.stall_minutes * 60
            cap = None if w.chat else (b.max_run_minutes if self._run_worktree.get(a_id) else b.max_coord_run_minutes)
            timed_out = cap is not None and t - started > cap * 60
            if not (stalled or timed_out):
                continue
            self._watchdog_reason[a_id] = "timeout" if timed_out else "stalled"
            self._watchdog_killed_at[a_id] = t
            runner.cancelled = True
            # Walk descendants before the group dies, or an orphaned child reparents to init
            # and the pid-ancestry walk can no longer find it.
            kill_descendants(proc.pid)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def _unstick_killed_run(self, a_id: str, runner: Runner, t: float) -> None:
        """A run we already killed can still be blocked in _stream()'s readline(): a daemonized
        grandchild can inherit the stdout/stderr pipe, reparent to init, and never exit, so the
        pipe never sees EOF even though the backend itself is dead. We can't kill that holder (not
        a verified descendant — never by name), so once it's had a while to exit on its own, force
        EOF on both pipes to unblock _run's completion handling instead."""
        killed_at = self._watchdog_killed_at.get(a_id)
        if killed_at is None or t - killed_at < 20:
            return
        proc = runner.proc
        if proc is None:
            return
        for stream in (proc.stdout, proc.stderr):
            if stream is not None and not stream.at_eof():
                stream.feed_eof()

    def sweep_zombie_runs(self) -> None:
        """REQ-ENG-050: a run marked 'running' whose agent this engine instance isn't actually
        tracking anymore (its process/task vanished without going through _run's own completion,
        e.g. after an engine crash-restart) becomes 'interrupted', same as the startup sweep
        (REQ-ENG-004), but re-checked every tick instead of only once at start."""
        s = self.store
        for run in s.q("SELECT id, agent FROM runs WHERE status='running'"):
            if run["agent"] in self.running:
                continue
            key = f"run_mail.{run['id']}"
            self._requeue_or_deliver_read(s.kv_get(key, []))
            s.x("DELETE FROM kv WHERE key=?", key)
            s.x("UPDATE runs SET status='interrupted', ended=? WHERE id=?", now(), run["id"])
            row = s.agent(run["agent"]) or {}
            if row.get("current_run") == run["id"]:
                s.set_agent(run["agent"], state="idle", current_run=None, activity="")

    async def _run(self, runner: Runner, spec: RunSpec, w: Wake, run_id: int, msgs: list[dict],
                   task: dict | None, session_version: int = 0) -> None:
        a, s = spec.agent, self.store
        limited = False

        def emit(kind: str, text: str) -> None:
            nonlocal limited
            self._last_output[a.id] = now()  # any line resets the stall clock (REQ-ENG-050)
            if kind == "backend_limit":
                limited = True
                info = json.loads(text)
                self.record_limit(a.backend, info["until"], info.get("reported", True))
                return
            if kind == "ratelimit":
                try:
                    s.kv_set("claude_ratelimit", {**json.loads(text), "at": now()})
                except (ValueError, TypeError):
                    pass
                return
            s.run_line(run_id, kind, text)
            if kind in ("tool", "text"):
                s.set_agent(a.id, activity=" ".join(text.split())[:160])

        try:
            await self.setup_worktree(runner, spec, task, run_id)
            if runner.cancelled:
                from .runners import RunResult
                res = RunResult(ok=False, error="stopped during setup")
            else:
                # setup_worktree may have appended failure context to spec.prompt; re-persist so the
                # inspector shows exactly what's about to be sent. The launch()-time write already
                # covers a crash during setup itself (nothing to update to in that case).
                s.update_run_prompt(run_id, spec.prompt)
                res = await runner.run(spec, emit)
        except asyncio.CancelledError:
            from .runners import RunResult
            runner.kill()
            error = "interrupted" if (s.kv_get("stopped") or self._stop.is_set()) else "service shutdown"
            res = RunResult(ok=False, error=error)
        except Exception as e:
            from .runners import RunResult
            res = RunResult(ok=False, error=repr(e))
            emit("error", traceback.format_exc()[-800:])
        finally:
            self.running.pop(a.id, None)
            self._run_started.pop(a.id, None)
            self._last_output.pop(a.id, None)
            self._run_worktree.pop(a.id, None)
            self._watchdog_killed_at.pop(a.id, None)
        watchdog_reason = self._watchdog_reason.pop(a.id, None)
        if res.extra.get("limit_until"):
            limited = True
            self.record_limit(a.backend, res.extra["limit_until"], res.extra.get("limit_reported", True))
        elif not limited and not res.ok and a.backend in ("claude", "codex") and usage_limit(res.error):
            limited = True
            reset = reported_reset({"message": res.error}, now())
            self.record_limit(a.backend, now() + 900 if reset is None else reset, reset is not None)
        status = "limited" if limited else "ok" if res.ok else (watchdog_reason or (
            ("interrupted" if (s.kv_get("stopped") or self._stop.is_set()) else "stopped") if runner.cancelled else "failed"))
        summary = (res.final_text or res.error or "").strip()
        s.end_run(run_id, status, res.cost, res.tokens, summary[:4000])
        s.x("DELETE FROM kv WHERE key=?", f"run_mail.{run_id}")
        row = s.agent(a.id) or {}
        fields = dict(state="idle", current_run=None, activity="", runs=(row.get("runs") or 0) + 1,
                      cost=(row.get("cost") or 0) + res.cost, tokens=(row.get("tokens") or 0) + res.tokens)
        if session_version != self._session_versions.get(a.id, 0):
            s.kv_set(f"local_history:{a.id}", [])
        if res.session_id and self.cfg.agent(a.id) and session_version == self._session_versions.get(a.id, 0):
            fields["session_id"] = res.session_id
            fields["session_runs"] = (row.get("session_runs") or 0) + 1
        s.set_agent(a.id, **fields)

        if a.backend == "codex":
            # Must come after end_run()/set_agent() above, not between running.pop() (in the
            # finally above) and end_run(): this awaits (a thread hop), and #61's
            # sweep_zombie_runs() runs every tick — if a tick landed while this run's row was
            # still 'running' but its agent already missing from self.running, the sweep would
            # flip it to interrupted and requeue its mail, which _run would then silently
            # overwrite. Placed here, the run row and agent fields are already finalized before
            # the first await, so every path below (limited, failed, ok) still passes through it.
            from .runners import codex_home_dir
            from .usage import apply_usage, rollout_usage
            sample = await asyncio.to_thread(
                rollout_usage, [res.session_id] if res.session_id else [], codex_home_dir(self.cfg, a.id))
            apply_usage(s, sample, self.record_limit)

        if limited:
            self._requeue_or_deliver_read([m["id"] for m in msgs])
            reset = time.strftime("%H:%M", time.localtime(s.kv_get(f"limit.{a.backend}")))
            s.event(a.id, "run", f"{a.backend.title()} limited until {reset}", significant=False)
            return
        if watchdog_reason and not self._stop.is_set():
            n = self.failures.get(a.id, (0, 0))[0] + 1
            self.failures[a.id] = (n, now() + min(600, 30 * 2 ** (n - 1)))
            self._requeue_or_deliver_read([m["id"] for m in msgs])  # retry the mail later, unless it's since been handled
            detail = (f"produced no output for {self.cfg.budget.stall_minutes:.0f}m" if watchdog_reason == "stalled"
                      else "ran past its time limit")
            s.event(a.id, watchdog_reason, f"{a.id}'s run {detail} and was killed ({n}x)")
            return
        if not res.ok and not runner.cancelled:
            n = self.failures.get(a.id, (0, 0))[0] + 1
            self.failures[a.id] = (n, now() + min(600, 30 * 2 ** (n - 1)))
            self._requeue_or_deliver_read([m["id"] for m in msgs])  # retry the mail later, unless it's since been handled
            s.event(a.id, "error", f"{a.id}'s run failed ({n}x): {res.error[:200]}", significant=False)
            if w.chat:
                s.send(a.id, "human", f"_(I hit an error and will retry shortly: {res.error[:300]})_", kind="chat")
            return
        self.failures.pop(a.id, None)
        if runner.cancelled:
            self._requeue_or_deliver_read([m["id"] for m in msgs])
            s.event(a.id, "run", f"{a.id}'s run was stopped", significant=False)
            return

        if w.chat and res.final_text.strip():
            s.send(a.id, "human", res.final_text.strip(), kind="chat")
        s.event(a.id, "run", f"{a.id} finished ({w.reason}){': ' + summary.splitlines()[0][:140] if summary else ''}",
                ref=f"run:{run_id}", significant=False)

        # Task bookkeeping: a task session that didn't finish backs off, then escalates.
        if w.reason == "task" and task:
            t = s.task(task["id"])
            if t and t["status"] == "in_progress" and t["assignee"] == a.id:
                attempts = (t["attempts"] or 0) + 1
                if attempts >= self.cfg.budget.max_task_attempts:
                    s.update_task(t["id"], actor="system", status="blocked", attempts=attempts,
                                  event_text=f"#{t['id']} blocked after {attempts} sessions without completion")
                    lead = next(x.id for x in self.cfg.agents if x.role == "lead")
                    s.send("system", lead, f"Task #{t['id']} ({t['title']}) went {attempts} sessions without being "
                           f"completed by {a.id}. It is now blocked. Please investigate: re-scope, split, or unblock.",
                           subject=f"#{t['id']} stuck", task_id=t["id"])
                else:
                    s.update_task(t["id"], attempts=attempts, next_attempt_at=now() + 45 * attempts)

        # Docs/specs written in the main tree get committed so builders branch from them.
        if self.cfg.git_autocommit and spec.cwd == self.cfg.root and not get_role(a.role).works_in_task_tree:
            from .gates import hold_main
            await asyncio.to_thread(hold_main, self.cfg, s, a.id, run_id)
            changed = await asyncio.to_thread(self._autocommit, a, summary)
            if changed:
                s.event(a.id, "commit", f"{a.id} updated {changed}", ref=f"run:{run_id}")

    def _autocommit(self, a: AgentCfg, summary: str) -> str:
        root = self.cfg.root
        try:
            files = [ln[3:] for ln in gitops.git(root, "status", "--porcelain").splitlines() if ln.strip()]
        except gitops.GitError:
            return ""
        if not files:
            return ""
        headline = (summary.splitlines() or ["update"])[0][:72]
        if gitops.autocommit_main(root, f"troupe({a.id}): {headline}\n\n{summary[:2000]}"):
            shown = ", ".join(files[:4]) + (f" +{len(files) - 4} more" if len(files) > 4 else "")
            return shown
        return ""

    # ── prompts ───────────────────────────────────────────────────────────
    def system_prompt(self, a: AgentCfg) -> str:
        role = get_role(a.role)
        names = HandleBook(self.cfg.project, self.cfg.agents)
        roster = "\n".join(
            f"- {names.name(x.id)}: {x.name}, {get_role(x.role).title} — {get_role(x.role).blurb}"
            + (" (you)" if x.id == a.id else "") for x in self.cfg.agents)
        return CHARTER.format(name=a.name, agent_id=names.name(a.id), title=role.title, project=self.cfg.project,
                              root=self.cfg.root, roster=roster) + "\n" + role.prompt

    def build_prompt(self, a: AgentCfg, w: Wake, msgs: list[dict], task: dict | None, row: dict) -> str:
        s = self.store
        names = HandleBook(self.cfg.project, self.cfg.agents)
        p: list[str] = [f"# Wake-up · {time.strftime('%Y-%m-%d %H:%M')} · {REASONS[w.reason]}"]
        for label, group in (("New messages", [m for m in msgs if not fyi_message(m, a, s)]),
                             ("FYI since last time", [m for m in msgs if fyi_message(m, a, s)])):
            if group:
                p.append(f"\n## {label} ({len(group)})")
                for m in group:
                    tag = " [LIVE CHAT]" if m["sender"] == "human" and m["kind"] == "chat" else ""
                    p.append(fmt_message(m, names) + tag + "\n")
        if task:
            label = "Task to review" if w.reason == "review" else "Your current task"
            p.append(f"\n## {label}\n" + fmt_task_full(s, task, names))
        open_tasks = s.tasks(OPEN_STATUSES)
        others = [t for t in open_tasks if t["assignee"] == a.id and (not task or t["id"] != task["id"])]
        if others:
            p.append("\n## Your other open tasks\n" + "\n".join(fmt_task_line(t, names) for t in others))
        if a.role in ("lead", "pm", "gadfly", "spec"):
            done = s.tasks(("done",), limit=400)
            p.append(f"\n## Board ({len(open_tasks)} open, {len(done)} done)\n"
                     + ("\n".join(fmt_task_line(t, names) for t in open_tasks[:60]) or "(empty board)"))
        if a.role in ('lead', 'pm'):
            milestones = [m for m in s.milestones() if m['status'] == 'active']
            if milestones:
                p.append("\n## Active milestones\n" + "\n".join(
                    f"- #{m['id']} {m['name']}: {m['done']}/{m['total']} done — {m['goal']}" for m in milestones))
        pending_q = s.q("SELECT * FROM questions WHERE asker=? AND status='open'", a.id)
        if pending_q:
            heading = ("Your open questions (did the human just answer one? if so, resolve_question)"
                       if w.chat else "Your questions still awaiting the human (don't re-ask)")
            p.append(f"\n## {heading}\n"
                     + "\n".join(f"- #{q['id']}: {q['question']}" for q in pending_q))
        pinned = s.memories(limit=50, include_private_of=a.id, pinned_only=True)
        if pinned:
            p.append("\n## Pinned memories\n" + "\n".join(
                f"- [{m['kind']} #{m['id']}] {m['title']}" + (f" — why: {m['rationale'][:160]}" if m["rationale"] else "")
                for m in pinned))
        pinned_ids = {m["id"] for m in pinned}
        decisions = [m for m in s.memories(kind="decision", limit=10, include_private_of=a.id)
                    if m["id"] not in pinned_ids]
        if decisions:
            p.append("\n## Recent team decisions\n" + "\n".join(
                f"- {m['title']} ({names.name(m['agent'])}, {ago(m['ts'])})" + (f" — why: {m['rationale'][:160]}" if m["rationale"] else "")
                for m in decisions))
        notes = s.q("SELECT * FROM memories WHERE agent=? AND scope='private' ORDER BY id DESC LIMIT 6", a.id)
        if notes:
            p.append("\n## Your private notes\n" + "\n".join(f"- {m['title']}: {m['content'][:200]}" for m in notes))
        team = [f"- {names.name(x['id'])}: {x['state']}" + (f" · {x['status']}" if x["status"] else "")
                for x in s.agents() if x["id"] != a.id]
        p.append("\n## Team right now\n" + "\n".join(team))
        if w.reason in ("proactive", "poke"):
            since = row.get("last_event_seen") or 0
            evs = [e for e in reversed(s.events(limit=40, after=since)) if e["agent"] != a.id and e["significant"]]
            if evs:
                p.append("\n## What happened since you last looked\n"
                         + "\n".join(f"- {ago(e['ts'])}: {names.event_text(e['text'])}" for e in evs[-25:]))
        p.append("\n## Now\n" + self.instruction(a, w, task, bool(msgs)))
        p.append("\nPrinciple 0 applies: the human comes first.")
        return "\n".join(p)

    def instruction(self, a: AgentCfg, w: Wake, task: dict | None, has_msgs: bool) -> str:
        role = get_role(a.role)
        if w.reason == "chat":
            extra = ""
            if a.role == "pm" and not self.store.memories(limit=1):
                extra = ("\nThis may be the very start of the project. Get to know what the human wants to build "
                         "before writing anything down; ask a few sharp questions at a time.")
            return ("The human is talking with you live in the troupe app (messages marked [LIVE CHAT]). "
                    "Your FINAL message text in this session is shown to them as your chat reply — write it "
                    "conversationally in markdown, concise, ending with the most useful next question if you "
                    "need input. Act on what they said with your tools first (update docs, brief teammates, "
                    "record decisions with remember). Check your open questions against what the human just said; "
                    "if they answered one, call resolve_question with their answer, then remember the decision. "
                    "File any new decision question with ask_human (with options) as well as asking in chat. "
                    "Don't also send_message the human the same content."
                    + extra)
        if w.reason == "review" and task:
            stat = gitops.diffstat(self.cfg.root, task["branch"]) if task.get("branch") else ""
            return (f"Review task #{task['id']}. Your cwd is its worktree (branch {task.get('branch')}).\n"
                    f"Diff vs main:\n{stat or '(unavailable)'}\n\nVerify by effect against the acceptance criteria "
                    f"and cited specs (run tests, run the thing), then call review_task({task['id']}, "
                    "\"approve\"|\"reject\", notes).")
        if w.reason == "task" and task:
            where = (f"You're in its worktree ({task['worktree']}, branch {task['branch']}). "
                     if task.get("worktree") else "")
            rejected = ("QA/merge feedback on a previous attempt is in the task's review notes — address it first. "
                        if task.get("review_notes") else "")
            cont = "You worked on this before; pick up where you left off. " if task.get("attempts") else ""
            return (f"Work on task #{task['id']}. {where}{rejected}{cont}When the acceptance criteria are met and "
                    f"verified, call complete_task({task['id']}, summary). If blocked, "
                    f"update_task({task['id']}, status=\"blocked\", note=...) and message whoever can unblock you.")
        no_task = ""
        if role.works_in_task_tree and not (task and task.get("worktree")):
            no_task = (" You have no active task, so your cwd is the main checkout: do NOT edit files here "
                       "(code changes only happen in task worktrees). Reply, or ask the lead for work.")
        if w.reason == "messages":
            return no_task + ("Handle your new messages: answer questions, act on requests, reply where a reply is expected "
                    "(send_message with reply_to). Record any decisions. Then stop."
                    + (" If you have a current task, you may continue it afterwards." if task else ""))
        base = (role.proactive or "Check whether anything needs your attention. If not, stop.") + no_task
        if has_msgs:
            base = "Handle your new messages first. " + base
        return base
