"""The orchestrator engine: decides who wakes up, why, and with what context — then runs them."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import traceback
from dataclasses import dataclass
from collections import deque
from pathlib import Path

from . import gitops, config as config_mod
from .config import AgentCfg, Config
from .roles import CHARTER, get_role
from .runners import RunSpec, make_runner, Runner, usage_limit, reported_reset
from .store import OPEN_STATUSES, Store, now
from .team import ago, fmt_message, fmt_task_full, fmt_task_line

TICK = 1.0
MESSAGE_DEBOUNCE = 2.0  # let bursts of mail land before waking someone
CHAT_DEBOUNCE = 0.4


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


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.running: dict[str, tuple[Runner, asyncio.Task, Wake]] = {}
        self.failures: dict[str, tuple[int, float]] = {}  # agent -> (count, retry_after)
        self.pokes: set[str] = set()
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
        s.x("UPDATE runs SET status='interrupted', ended=? WHERE status='running'", now())
        s.x("UPDATE agents SET state='idle', current_run=NULL, activity='' WHERE state='running'")
        for a in self.cfg.agents:
            s.set_agent(a.id, enabled=int(a.enabled))
        s.kv_set("checking_task", None)
        self.cleanup_worktrees()

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
        self.recover()
        self.store.event("system", "engine", "Engine started", significant=False)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                self.store.event("system", "error", "engine tick failed: " + traceback.format_exc()[-600:],
                                 significant=False)
            await asyncio.sleep(TICK)
        for runner, _task, _w in list(self.running.values()):
            runner.kill()
        await asyncio.sleep(0.5)
        if self._merge_task is not None:
            await self._merge_task

    # ── the loop ──────────────────────────────────────────────────────────
    async def tick(self) -> None:
        s = self.store
        s.kv_set("heartbeat", now())
        self.reload_config()
        self.handle_commands()
        if self._merge_task is not None and self._merge_task.done():
            try:
                self._merge_task.result()
            except Exception as e:
                s.event("system", "error", f"Merge worker failed: {e}", significant=False)
            self._merge_task = None
        if self._merge_task is None:
            self._merge_task = asyncio.create_task(asyncio.to_thread(self.process_approved))
        paused = bool(s.kv_get("paused", False))
        if not paused:
            self.dispatch()
        wakes = sorted(self.candidates(paused), key=lambda w: w.priority)
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
            if cmd == "stop" and arg in self.running:
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
                return sum(1 for x in open_tasks if x["assignee"] == a.id and x["status"] in ("ready", "in_progress"))

            best = min(pool, key=lambda a: (load(a), a.id in self.running))
            if len(pool) > 1 and load(best) > 0 and get_role(t["role"]).works_in_task_tree:
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
        self.store.update_task(t["id"], actor=a.id, event_text=f"{a.id} started #{t['id']} {t['title']}", **fields)
        return self.store.task(t["id"]) or t

    def process_approved(self) -> None:
        if not self._merge_lock.acquire(blocking=False):
            return
        try:
            self._process_approved()
        finally:
            self.store.kv_set("checking_task", None)
            self._merge_lock.release()

    def check_failed(self, task: dict, output: str) -> None:
        note = f"Checks failed on #{task['id']}:\n{output}"
        self.store.update_task(task["id"], actor="system", status="in_progress", next_attempt_at=0,
                               review_notes=note, event_text=f"Checks failed on #{task['id']}")
        self.store.kv_set(f"check_failed.{task['id']}", True)
        if task["assignee"]:
            self.store.send("system", task["assignee"], note + "\nFix the failure and call complete_task for QA review.",
                            subject=f"#{task['id']} checks failed", task_id=task["id"], kind="system")

    def _process_approved(self) -> None:
        s = self.store
        for t in s.tasks(("approved",)):
            if self._stop.is_set():
                return
            if any(w.task and w.task["id"] == t["id"] for _, _, w in list(self.running.values())):
                continue
            if not t["branch"]:
                s.update_task(t["id"], actor="system", status="done", event_text=f"#{t['id']} done")
                continue
            cfg = self.cfg
            if cfg.git.check:
                s.kv_set("checking_task", t["id"])
                tree = Path(t["worktree"]) if t["worktree"] else None
                try:
                    if tree is None or not tree.exists():
                        self.check_failed(t, "Task worktree is missing; restore it and resubmit.")
                        continue
                    main_head, task_head = gitops.prepare_check(cfg.root, tree, t["branch"])
                    log_path = cfg.state_dir / "checks" / f"t{t['id']}.log"
                    passed, outcome = gitops.run_check(tree, cfg.git.check, cfg.git.check_timeout, log_path, self._stop)
                    current = s.task(t["id"])
                    if self._stop.is_set() or not current or current["status"] != "approved":
                        continue
                    if not passed:
                        with log_path.open(errors="replace") as log:
                            tail = "".join(deque(log, maxlen=50))[-12000:]
                        self.check_failed(t, tail or outcome)
                        continue
                    ok, out = gitops.merge_checked(cfg.root, tree, t["branch"], main_head, task_head,
                                                   f"Merge #{t['id']}: {t['title']}", self._stop)
                    if self._stop.is_set():
                        continue
                    if not ok and out.startswith("Repository changed"):
                        with log_path.open("a") as log:
                            log.write(out + "\n")
                        self.check_failed(t, out)
                        continue
                except gitops.GitError as e:
                    ok, out = False, str(e)
                except OSError as e:
                    self.check_failed(t, f"Could not run checks: {e}")
                    continue
                finally:
                    s.kv_set("checking_task", None)
            else:
                ok, out = gitops.merge_branch(cfg.root, t["branch"], f"Merge #{t['id']}: {t['title']}")
            if ok:
                try:
                    if t["worktree"]:
                        gitops.remove_worktree(self.cfg.root, Path(t["worktree"]))
                    gitops.delete_branch(self.cfg.root, t["branch"])
                except gitops.GitError as e:
                    s.event("system", "error", f"Merged #{t['id']}, but cleanup failed: {e}", significant=False)
                s.update_task(t["id"], actor="system", status="done", worktree=None,
                              event_text=f"Merged #{t['id']} {t['title']} into main")
                if t["assignee"]:
                    s.send("system", t["assignee"], f"Your task #{t['id']} passed review and was merged into main.",
                           subject=f"#{t['id']} merged", task_id=t["id"], kind="system")
                    s.mark_read([m["id"] for m in s.unread(t["assignee"]) if m["kind"] == "system"])
            else:
                s.update_task(t["id"], actor="system", status="in_progress", next_attempt_at=0,
                              review_notes=f"Merge conflict with main:\n{out[-1500:]}",
                              event_text=f"Merge conflict on #{t['id']} — back to {t['assignee']}")
                if t["assignee"]:
                    base = gitops.current_branch(self.cfg.root)
                    s.send("system", t["assignee"],
                           f"QA approved #{t['id']} but it conflicts with {base}. In your worktree run "
                           f"`git merge {base}`, resolve the conflicts, re-run the tests, commit, then "
                           f"complete_task again.\n\n{out[-1200:]}",
                           subject=f"#{t['id']} merge conflict", task_id=t["id"], kind="system")

    def backend_limited(self, backend: str) -> bool:
        return (self.store.kv_get(f"limit.{backend}", 0) or 0) > now()

    def record_limit(self, backend: str, until: float, reported: bool = True) -> None:
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
        self.store.kv_set(f"limit_meta.{backend}", {"until": until, "reported": reported})

    # ── who should wake ───────────────────────────────────────────────────
    def candidates(self, paused: bool) -> list[Wake]:
        s = self.store
        t_now = now()
        out: list[Wake] = []
        db_agents = {a["id"]: a for a in s.agents()}
        review_queue = [t for t in s.tasks(("review",))]
        for a in self.cfg.agents:
            row = db_agents.get(a.id)
            if a.id in self.running or not row or not row["enabled"] or self.backend_limited(a.backend):
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
            if paused:
                continue
            if a.id in self.pokes:
                out.append(Wake(1, a, "poke", self.current_task(a)))
                continue
            if a.role == "qa":
                mine = [t for t in review_queue if t["reviewer"] in (None, a.id)]
                if mine:
                    out.append(Wake(1, a, "review", mine[0]))
                    continue
            real_mail = [m for m in unread if m["kind"] != "system" or m["sender"] != "system"]
            if unread and t_now - unread[-1]["ts"] >= MESSAGE_DEBOUNCE and real_mail:
                out.append(Wake(2, a, "messages", self.current_task(a)))
                continue
            task = self.current_task(a)
            if task and task["status"] in ("ready", "in_progress") and t_now >= (task["next_attempt_at"] or 0):
                out.append(Wake(3, a, "task", task))
                continue
            if (a.idle_seconds > 0 and t_now - (row["last_run_at"] or 0) >= a.idle_seconds
                    and s.world_changed_for(a.id, row["last_event_seen"] or 0)):
                out.append(Wake(5, a, "proactive", task))
        return out

    # ── running an agent ──────────────────────────────────────────────────
    async def launch(self, w: Wake) -> None:
        a, s = w.agent, self.store
        if self.backend_limited(a.backend):
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
        s.mark_read([m["id"] for m in msgs])
        if any(m["sender"] == "human" and m["kind"] == "chat" for m in msgs):
            w.reason = "chat"  # whatever woke them, the human gets a live reply
        row = s.agent(a.id) or {}
        seen = s.max_event_id()
        prompt = self.build_prompt(a, w, msgs, task, row)
        system = self.system_prompt(a)
        run_id = s.start_run(a.id, w.reason, task["id"] if task else None, str(cwd), w.chat,
                             prompt=prompt, system=system)
        s.set_agent(a.id, state="running", current_run=run_id, activity=REASONS[w.reason], last_run_at=now(),
                    last_event_seen=seen)
        s.event(a.id, "run", f"{a.id} woke up: {REASONS[w.reason]}", ref=f"run:{run_id}", significant=False)
        runner = make_runner(a.backend)
        spec = RunSpec(cfg=self.cfg, agent=a, system=system, prompt=prompt, cwd=cwd,
                       session_id=row.get("session_id"), log_path=self.cfg.runs_dir / f"{run_id:06d}-{a.id}.jsonl")
        atask = asyncio.create_task(self._run(runner, spec, w, run_id, msgs, task, self._session_versions.get(a.id, 0)))
        self.running[a.id] = (runner, atask, w)

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

    async def _run(self, runner: Runner, spec: RunSpec, w: Wake, run_id: int, msgs: list[dict],
                   task: dict | None, session_version: int = 0) -> None:
        a, s = spec.agent, self.store
        limited = False

        def emit(kind: str, text: str) -> None:
            nonlocal limited
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
        except Exception as e:
            from .runners import RunResult
            res = RunResult(ok=False, error=repr(e))
            emit("error", traceback.format_exc()[-800:])
        finally:
            self.running.pop(a.id, None)
        if res.extra.get("limit_until"):
            limited = True
            self.record_limit(a.backend, res.extra["limit_until"], res.extra.get("limit_reported", True))
        elif not limited and not res.ok and a.backend in ("claude", "codex") and usage_limit(res.error):
            limited = True
            reset = reported_reset({"message": res.error}, now())
            self.record_limit(a.backend, now() + 900 if reset is None else reset, reset is not None)
        status = "limited" if limited else "ok" if res.ok else ("stopped" if runner.cancelled else "failed")
        summary = (res.final_text or res.error or "").strip()
        s.end_run(run_id, status, res.cost, res.tokens, summary[:4000])
        row = s.agent(a.id) or {}
        fields = dict(state="idle", current_run=None, activity="", runs=(row.get("runs") or 0) + 1,
                      cost=(row.get("cost") or 0) + res.cost, tokens=(row.get("tokens") or 0) + res.tokens)
        if session_version != self._session_versions.get(a.id, 0):
            s.kv_set(f"local_history:{a.id}", [])
        if res.session_id and self.cfg.agent(a.id) and session_version == self._session_versions.get(a.id, 0):
            fields["session_id"] = res.session_id
            fields["session_runs"] = (row.get("session_runs") or 0) + 1
        s.set_agent(a.id, **fields)

        if limited:
            s.mark_unread([m["id"] for m in msgs])
            reset = time.strftime("%H:%M", time.localtime(s.kv_get(f"limit.{a.backend}")))
            s.event(a.id, "run", f"{a.backend.title()} limited until {reset}", significant=False)
            return
        if not res.ok and not runner.cancelled:
            n = self.failures.get(a.id, (0, 0))[0] + 1
            self.failures[a.id] = (n, now() + min(600, 30 * 2 ** (n - 1)))
            s.mark_unread([m["id"] for m in msgs])  # retry the mail later
            s.event(a.id, "error", f"{a.id}'s run failed ({n}x): {res.error[:200]}", significant=False)
            if w.chat:
                s.send(a.id, "human", f"_(I hit an error and will retry shortly: {res.error[:300]})_", kind="chat")
            return
        self.failures.pop(a.id, None)
        if runner.cancelled:
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
        roster = "\n".join(
            f"- {x.id}: {x.name}, {get_role(x.role).title} — {get_role(x.role).blurb}"
            + (" (you)" if x.id == a.id else "") for x in self.cfg.agents)
        return CHARTER.format(name=a.name, agent_id=a.id, title=role.title, project=self.cfg.project,
                              root=self.cfg.root, roster=roster) + "\n" + role.prompt

    def build_prompt(self, a: AgentCfg, w: Wake, msgs: list[dict], task: dict | None, row: dict) -> str:
        s = self.store
        p: list[str] = [f"# Wake-up · {time.strftime('%Y-%m-%d %H:%M')} · {REASONS[w.reason]}"]
        if msgs:
            p.append(f"\n## New messages ({len(msgs)})")
            for m in msgs:
                tag = " [LIVE CHAT]" if m["sender"] == "human" and m["kind"] == "chat" else ""
                p.append(fmt_message(m) + tag + "\n")
        if task:
            label = "Task to review" if w.reason == "review" else "Your current task"
            p.append(f"\n## {label}\n" + fmt_task_full(s, task))
        open_tasks = s.tasks(OPEN_STATUSES)
        others = [t for t in open_tasks if t["assignee"] == a.id and (not task or t["id"] != task["id"])]
        if others:
            p.append("\n## Your other open tasks\n" + "\n".join(fmt_task_line(t) for t in others))
        if a.role in ("lead", "pm", "gadfly", "spec"):
            done = s.tasks(("done",), limit=400)
            p.append(f"\n## Board ({len(open_tasks)} open, {len(done)} done)\n"
                     + ("\n".join(fmt_task_line(t) for t in open_tasks[:60]) or "(empty board)"))
        pending_q = s.q("SELECT * FROM questions WHERE asker=? AND status='open'", a.id)
        if pending_q:
            heading = ("Your open questions (did the human just answer one? if so, resolve_question)"
                       if w.chat else "Your questions still awaiting the human (don't re-ask)")
            p.append(f"\n## {heading}\n"
                     + "\n".join(f"- #{q['id']}: {q['question']}" for q in pending_q))
        decisions = s.memories(kind="decision", limit=10, include_private_of=a.id)
        if decisions:
            p.append("\n## Recent team decisions\n" + "\n".join(
                f"- {m['title']} ({m['agent']}, {ago(m['ts'])})" + (f" — why: {m['rationale'][:160]}" if m["rationale"] else "")
                for m in decisions))
        notes = s.q("SELECT * FROM memories WHERE agent=? AND scope='private' ORDER BY id DESC LIMIT 6", a.id)
        if notes:
            p.append("\n## Your private notes\n" + "\n".join(f"- {m['title']}: {m['content'][:200]}" for m in notes))
        team = [f"- {x['id']}: {x['state']}" + (f" · {x['status']}" if x["status"] else "")
                for x in s.agents() if x["id"] != a.id]
        p.append("\n## Team right now\n" + "\n".join(team))
        if w.reason in ("proactive", "poke"):
            since = row.get("last_event_seen") or 0
            evs = [e for e in reversed(s.events(limit=40, after=since)) if e["agent"] != a.id and e["significant"]]
            if evs:
                p.append("\n## What happened since you last looked\n"
                         + "\n".join(f"- {ago(e['ts'])}: {e['text']}" for e in evs[-25:]))
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
