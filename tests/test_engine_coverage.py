"""Behavioral coverage for shipped engine requirements; no live model calls."""
import asyncio
from pathlib import Path

import pytest

from troupe import cli, gitops
from troupe.engine import Engine, Wake
from troupe.roles import get_role
from troupe.runners import RunResult
from troupe.store import Store
from troupe.team import TeamAPI


class FakeRunner:
    cancelled = False

    def __init__(self, result):
        self.result = result
        self.specs = []

    async def run(self, spec, emit):
        self.specs.append(spec)
        emit("text", "working")
        return self.result


async def run_wake(engine, wake):
    await engine.launch(wake)
    await engine.running[wake.agent.id][1]


def test_cli_starts_detached_and_attaches(project, monkeypatch):
    import argparse
    from troupe import service
    from troupe.gui import app
    cfg, _ = project
    actions = []
    monkeypatch.setattr(cli.config_mod, "find_root", lambda: cfg.root)
    monkeypatch.setattr(service, "start_service", lambda cfg: actions.append("start"))
    monkeypatch.setattr(app, "run_gui", lambda cfg: actions.append("attach"))
    cli.cmd_up(argparse.Namespace())
    assert actions == ["start", "attach"]


def test_shared_store_commands_recovery_and_heartbeat(project):
    cfg, store = project
    other = Store(cfg.db_path)
    assert other.scalar("PRAGMA journal_mode") == "wal"
    rid = store.start_run("pm", "poke", None, str(cfg.root), False)
    store.set_agent("pm", state="running", current_run=rid)
    engine = Engine(cfg)
    engine.recover()
    assert other.runs()[0]["status"] == "interrupted"
    assert other.agent("pm")["state"] == "idle"
    other.command("poke", "pm")
    engine.handle_commands()
    assert "pm" in engine.pokes
    assert other.pending_commands() == []
    store.kv_set("paused", True)
    asyncio.run(engine.tick())
    assert other.kv_get("heartbeat") > 0


def test_wake_precedence_debounce_and_pause(project, monkeypatch):
    cfg, store = project
    monkeypatch.setattr("troupe.engine.now", lambda: 1000)
    engine = Engine(cfg)
    tid = store.add_task("Review", status="review")
    store.send("lead", "qa", "mail")
    store.x("UPDATE messages SET ts=997")
    def reason(paused=False):
        return next((w.reason for w in engine.candidates(paused) if w.agent.id == "qa"), None)
    assert reason() == "review"
    engine.pokes.add("qa")
    assert reason() == "poke"
    store.send("human", "qa", "hello", kind="chat")
    store.x("UPDATE messages SET ts=1000 WHERE sender='human'")
    assert reason() is None
    store.x("UPDATE messages SET ts=999 WHERE sender='human'")
    assert reason(True) == "chat"
    store.mark_read([m["id"] for m in store.unread("qa") if m["sender"] == "human"])
    assert reason(True) is None
    engine.pokes.clear()
    store.update_task(tid, status="done")
    assert reason() == "messages"
    store.x("UPDATE messages SET ts=999")
    assert reason() != "messages"


def test_proactive_requires_elapsed_cadence_and_external_change(project, monkeypatch):
    cfg, store = project
    a = cfg.agent("pm")
    monkeypatch.setattr("troupe.engine.now", lambda: 100000)
    engine = Engine(cfg)
    def eligible():
        return any(w.agent.id == a.id and w.reason == "proactive" for w in engine.candidates(False))
    assert not eligible()
    store.event("pm", "memory", "own change")
    assert not eligible()
    store.event("lead", "task", "external change")
    store.set_agent("pm", last_run_at=100000)
    assert not eligible()
    store.set_agent("pm", last_run_at=0)
    assert eligible()
    store.set_agent("pm", last_event_seen=store.max_event_id())
    assert not eligible()


def test_concurrency_reserves_two_extra_chat_slots(project, monkeypatch):
    cfg, _ = project
    cfg.budget.max_concurrent = 1
    engine = Engine(cfg)
    wakes = [Wake(0, cfg.agent(a), "chat") for a in ("pm", "spec", "lead", "gadfly")]
    wakes += [Wake(3, cfg.agent(a), "poke") for a in ("builder-1", "builder-2")]
    monkeypatch.setattr(engine, "candidates", lambda paused: wakes)
    async def launch(w):
        engine.running[w.agent.id] = (None, None, w)
    monkeypatch.setattr(engine, "launch", launch)
    asyncio.run(engine.tick())
    assert sum(w.chat for _, _, w in engine.running.values()) == 3
    assert sum(not w.chat for _, _, w in engine.running.values()) == 1


def test_budget_limits_ignore_chat_count_and_clear_throttle(project):
    cfg, store = project
    cfg.budget.max_runs_per_hour = 1
    cfg.budget.max_usd_per_day = 1
    engine = Engine(cfg)
    store.start_run("pm", "chat", None, str(cfg.root), True)
    assert engine.budget_ok()
    rid = store.start_run("spec", "task", None, str(cfg.root), False)
    assert not engine.budget_ok()
    assert "run limit" in store.kv_get("throttled")
    cfg.budget.max_runs_per_hour = 0
    store.end_run(rid, "ok", 1, 10, "done")
    assert not engine.budget_ok()
    assert "daily budget" in store.kv_get("throttled")
    cfg.budget.max_usd_per_day = 0
    assert engine.budget_ok()
    assert store.kv_get("throttled") == ""


def test_failures_requeue_mail_and_backoff_to_cap(project, monkeypatch):
    cfg, store = project
    monkeypatch.setattr("troupe.engine.now", lambda: 1000)
    runner = FakeRunner(RunResult(ok=False, error="offline"))
    monkeypatch.setattr("troupe.engine.make_runner", lambda backend: runner)
    engine = Engine(cfg)
    store.send("lead", "pm", "retry me")
    for attempt, delay in enumerate((30, 60, 120, 240, 480, 600, 600), 1):
        asyncio.run(run_wake(engine, Wake(2, cfg.agent("pm"), "messages")))
        assert engine.failures["pm"] == (attempt, 1000 + delay)
        assert store.unread("pm")[0]["body"] == "retry me"
        assert not any(w.agent.id == "pm" for w in engine.candidates(False))


def test_chat_consumed_by_other_wake_gets_final_reply(project, monkeypatch):
    cfg, store = project
    runner = FakeRunner(RunResult(ok=True, final_text="Here is the answer"))
    monkeypatch.setattr("troupe.engine.make_runner", lambda backend: runner)
    store.send("human", "pm", "question", kind="chat")
    asyncio.run(run_wake(Engine(cfg), Wake(1, cfg.agent("pm"), "poke")))
    assert store.unread("pm") == []
    assert store.chat_thread("pm")[-1]["body"] == "Here is the answer"
    assert store.runs()[0]["chat"] == 1


def test_prompts_include_role_and_context(project):
    cfg, store = project
    engine = Engine(cfg)
    a = cfg.agent("spec")
    tid = store.add_task("Current brief", description="Details", acceptance="Pass", assignee=a.id)
    store.add_task("Other brief", assignee=a.id)
    store.ask(a.id, "Pending question")
    store.remember("lead", "Team decision", rationale="Because")
    store.remember(a.id, "Private note", content="Own secret", scope="private")
    store.remember("pm", "Hidden secret", scope="private")
    store.send("lead", a.id, "Mail content")
    prompt = engine.build_prompt(a, Wake(5, a, "proactive"), store.unread(a.id), store.task(tid), {})
    for text in ("Current brief", "Details", "Pass", "Other brief", "Board", "Pending question",
                 "Team decision", "Because", "Own secret", "Mail content", "Team right now",
                 "What happened since you last looked", "## Now"):
        assert text in prompt
    assert "Hidden secret" not in prompt
    system = engine.system_prompt(a)
    assert get_role(a.role).prompt in system
    from troupe.store import HandleBook
    names = HandleBook(cfg.project, cfg.agents)
    assert all(names.name(agent.id) in system for agent in cfg.agents)


def test_prompt_excludes_superseded_and_shows_pinned_before_decisions(project):
    cfg, store = project
    engine = Engine(cfg)
    a = cfg.agent("spec")
    old_id = store.remember("lead", "Old decision", kind="decision")
    store.remember("lead", "New decision", kind="decision", supersedes=old_id)
    pinned_id = store.remember("lead", "Pinned fact", kind="fact")
    store.update_memory(pinned_id, pinned=True)
    prompt = engine.build_prompt(a, Wake(1, a, "messages"), [], None, {})
    assert "Old decision" not in prompt
    assert "New decision" in prompt
    assert "Pinned memories" in prompt and "Pinned fact" in prompt
    assert prompt.index("Pinned memories") < prompt.index("Recent team decisions")


def test_dispatch_waits_for_dependency_and_skips_disabled(project):
    cfg, store = project
    dep = store.add_task("Dependency")
    tid = store.add_task("Dependent", status="ready", depends_on=[dep])
    store.set_agent("builder-1", enabled=0)
    engine = Engine(cfg)
    engine.dispatch()
    assert store.task(tid)["assignee"] is None
    store.update_task(dep, status="done")
    engine.dispatch()
    assert store.task(tid)["assignee"] == "builder-2"


def test_worktree_review_reject_and_merge(project, monkeypatch):
    cfg, store = project
    engine = Engine(cfg)
    tid = store.add_task("Add file", status="ready", assignee="builder-1")
    task = engine.start_task(cfg.agent("builder-1"), store.task(tid))
    tree = Path(task["worktree"])
    assert tree == cfg.worktrees_dir / f"t{tid}"
    assert task["branch"].startswith(f"troupe/t{tid}-")
    (tree / "feature.txt").write_text("feature\n")
    builder = TeamAPI(cfg, store, "builder-1")
    qa = TeamAPI(cfg, store, "qa")
    assert "review" in builder.complete_task(tid, "Added file")
    assert gitops.git(tree, "status", "--porcelain") == ""
    runner = FakeRunner(RunResult(ok=True))
    monkeypatch.setattr("troupe.engine.make_runner", lambda backend: runner)
    asyncio.run(run_wake(engine, Wake(1, cfg.agent("qa"), "review", store.task(tid))))
    assert runner.specs[0].cwd == tree
    qa.review_task(tid, "reject", "Add coverage")
    assert store.task(tid)["status"] == "in_progress"
    assert "Add coverage" in store.unread("builder-1")[-1]["body"]
    builder.complete_task(tid, "Verified")
    qa.review_task(tid, "approve", "Pass")
    engine.process_approved()
    assert store.task(tid)["status"] == "done"
    assert (cfg.root / "feature.txt").read_text() == "feature\n"
    assert not tree.exists()
    assert len(gitops.git(cfg.root, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3


def test_conflict_aborts_and_returns_to_builder(project):
    cfg, store = project
    (cfg.root / "shared.txt").write_text("base\n")
    gitops.commit_all(cfg.root, "base")
    engine = Engine(cfg)
    tid = store.add_task("Conflict", status="ready", assignee="builder-1")
    task = engine.start_task(cfg.agent("builder-1"), store.task(tid))
    tree = Path(task["worktree"])
    (tree / "shared.txt").write_text("branch\n")
    TeamAPI(cfg, store, "builder-1").complete_task(tid, "change")
    (cfg.root / "shared.txt").write_text("main\n")
    gitops.commit_all(cfg.root, "main change")
    TeamAPI(cfg, store, "qa").review_task(tid, "approve", "pass")
    engine.process_approved()
    assert store.task(tid)["status"] == "in_progress"
    assert "Merge conflict" in store.task(tid)["review_notes"]
    assert "git merge main" in store.unread("builder-1")[-1]["body"]
    assert (cfg.root / "shared.txt").read_text() == "main\n"
    assert not (cfg.root / ".git" / "MERGE_HEAD").exists()
    assert tree.exists()


def test_unfinished_task_backs_off_then_escalates(project, monkeypatch):
    cfg, store = project
    cfg.budget.max_task_attempts = 2
    monkeypatch.setattr("troupe.engine.now", lambda: 1000)
    monkeypatch.setattr("troupe.engine.make_runner", lambda backend: FakeRunner(RunResult(ok=True)))
    engine = Engine(cfg)
    tid = store.add_task("Work", status="in_progress", assignee="builder-1")
    for _ in range(2):
        asyncio.run(run_wake(engine, Wake(3, cfg.agent("builder-1"), "task", store.task(tid))))
        if store.task(tid)["attempts"] == 1:
            assert store.task(tid)["next_attempt_at"] == 1045
    assert store.task(tid)["status"] == "blocked"
    assert "blocked" in store.unread("lead")[-1]["body"]


@pytest.mark.parametrize("agent,committed", [("builder-1", False), ("spec", True)])
def test_main_checkout_autocommit_policy(project, monkeypatch, agent, committed):
    cfg, _ = project
    cfg.git_autocommit = True
    monkeypatch.setattr("troupe.engine.make_runner", lambda backend: FakeRunner(RunResult(ok=True)))
    engine = Engine(cfg)
    a = cfg.agent(agent)
    if not committed:
        assert "do NOT edit" in engine.instruction(a, Wake(2, a, "messages"), None, True)
    (cfg.root / "note.txt").write_text("note")
    asyncio.run(run_wake(engine, Wake(2, a, "messages")))
    assert (gitops.git(cfg.root, "status", "--porcelain") == "") is committed
