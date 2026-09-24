"""Approved branches pass a real command before touching main."""
import asyncio
from pathlib import Path
import threading
import time

import pytest

from troupe import config, gitops
from troupe.engine import Engine
from troupe.gui.data import Data


def approved(project):
    cfg, store = project
    engine = Engine(cfg)
    tid = store.add_task("Gated feature", status="ready", assignee="builder-1")
    task = engine.start_task(cfg.agent("builder-1"), store.task(tid))
    tree = Path(task["worktree"])
    (tree / "feature.txt").write_text("feature")
    gitops.commit_all(tree, "Feature")
    (cfg.root / "main.txt").write_text("new main")
    gitops.commit_all(cfg.root, "Main advanced")
    store.update_task(tid, status="approved")
    return cfg, store, engine, tid, tree


def test_pass_checks_updated_worktree_then_merges(project):
    cfg, store, engine, tid, tree = approved(project)
    cfg.git.check = "test -f feature.txt && test -f main.txt && echo passed"
    engine.process_approved()
    assert store.task(tid)["status"] == "done"
    assert (cfg.root / "feature.txt").exists()
    assert not tree.exists()
    assert "passed" in (cfg.state_dir / "checks" / f"t{tid}.log").read_text()
    assert not store.kv_get("checking_task")
    assert len(gitops.git(cfg.root, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3


def test_failure_preserves_main_and_routes_through_qa(project):
    cfg, store, engine, tid, tree = approved(project)
    cfg.git.check = "i=0; while [ $i -lt 80 ]; do echo line-$i; i=$((i+1)); done; echo failure >&2; exit 7"
    before = gitops.git(cfg.root, "rev-parse", "HEAD")
    engine.process_approved()
    task = store.task(tid)
    assert task["status"] == "in_progress"
    assert task["assignee"] == "builder-1"
    assert "line-79" in task["review_notes"] and "failure" in task["review_notes"]
    assert "line-0\n" not in task["review_notes"]
    assert "failure" in store.unread("builder-1")[-1]["body"]
    assert gitops.git(cfg.root, "rev-parse", "HEAD") == before
    assert not (cfg.root / "feature.txt").exists()
    assert (tree / "main.txt").exists()
    assert "line-0\n" in (cfg.state_dir / "checks" / f"t{tid}.log").read_text()
    data = Data(cfg)
    data.refresh(force=True)
    assert next(t for t in data.tasks if t["id"] == tid)["merge_check"] == "checks failed"
    store.update_task(tid, status="review")
    store.update_task(tid, status="in_progress")
    data.refresh(force=True)
    assert not next(t for t in data.tasks if t["id"] == tid)["merge_check"]


def test_timeout_fails_and_kills_check(project):
    cfg, store, engine, tid, tree = approved(project)
    cfg.git.check = "echo starting; sleep 5; echo should-not-happen"
    cfg.git.check_timeout = 0.1
    started = time.monotonic()
    engine.process_approved()
    assert time.monotonic() - started < 3
    assert store.task(tid)["status"] == "in_progress"
    assert "timed out" in store.task(tid)["review_notes"]
    assert not (cfg.root / "feature.txt").exists()


def test_empty_gate_and_unbranched_task_skip_checks(project):
    cfg, store, engine, tid, tree = approved(project)
    engine.process_approved()
    assert store.task(tid)["status"] == "done"
    assert not (cfg.state_dir / "checks").exists()
    cfg.git.check = "exit 9"
    plain = store.add_task("Docs", status="approved")
    engine.process_approved()
    assert store.task(plain)["status"] == "done"


def test_precheck_conflict_bounces_without_running_command(project):
    cfg, store, engine, tid, tree = approved(project)
    (cfg.root / "feature.txt").write_text("conflicting main")
    gitops.commit_all(cfg.root, "Conflict")
    cfg.git.check = "echo should-not-run"
    before = gitops.git(cfg.root, "rev-parse", "HEAD")
    engine.process_approved()
    assert store.task(tid)["status"] == "in_progress"
    assert "Merge conflict" in store.task(tid)["review_notes"]
    assert gitops.git(cfg.root, "rev-parse", "HEAD") == before
    assert not (cfg.state_dir / "checks").exists()
    assert not gitops.git(tree, "rev-parse", "--verify", "MERGE_HEAD", check=False)


def test_repository_changes_during_check_do_not_merge(project):
    cfg, store, engine, tid, tree = approved(project)
    cfg.git.check = "echo unchecked > feature.txt"
    engine.process_approved()
    assert store.task(tid)["status"] == "in_progress"
    assert "Repository changed" in store.task(tid)["review_notes"]
    assert not (cfg.root / "feature.txt").exists()


def test_worker_does_not_block_ticks_or_chat_and_is_serial(project, monkeypatch):
    """Deterministic under load (#71): the merge worker runs in a real thread
    (asyncio.to_thread), so waiting for it to start is a real OS-scheduling race, not just
    cooperative asyncio ordering. `release.wait(30)` is a generous safety net, not a budget the
    test relies on — `release.set()` below fires as soon as the assertions pass, normally in
    well under a second. The mutation check (tick() must not block on the worker) only needs its
    own deadline to stay comfortably *shorter* than 30s; 10s leaves a wide margin over anything
    system load could plausibly add to an in-process tick(), while still failing fast and hard if
    tick() actually blocked on the still-running check (which wouldn't return for ~30s)."""
    cfg, store, engine, tid, tree = approved(project)
    cfg.git.check = "true"
    started, release = threading.Event(), threading.Event()
    calls = []
    def check(*args):
        calls.append(args)
        started.set()
        assert release.wait(30)
        return True, "passed"
    monkeypatch.setattr(gitops, "run_check", check)
    store.kv_set("paused", True)
    store.send("human", "pm", "hello", kind="chat")
    store.x("UPDATE messages SET ts=0")
    chats = []
    async def launch(wake):
        chats.append(wake.agent.id)
    monkeypatch.setattr(engine, "launch", launch)
    async def run():
        await engine.tick()
        assert await asyncio.to_thread(started.wait, 10)
        worker = engine._merge_task
        assert store.kv_get("checking_task") == tid
        await asyncio.wait_for(engine.tick(), timeout=10)
        assert engine._merge_task is worker
        assert chats and store.kv_get("heartbeat") > 0
        assert len(calls) == 1
        release.set()
        await worker
    asyncio.run(run())
    assert store.task(tid)["status"] == "done"


def test_stop_during_check_never_merges(project):
    cfg, store, engine, tid, tree = approved(project)
    cfg.git.check = "echo started; sleep 10"
    thread = threading.Thread(target=engine.process_approved)
    thread.start()
    for _ in range(100):
        if store.kv_get("checking_task"): break
        time.sleep(0.01)
    engine.stop()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert store.task(tid)["status"] == "approved"
    assert not (cfg.root / "feature.txt").exists()
    assert not store.kv_get("checking_task")


def test_check_config_validation(project):
    cfg, _ = project
    path = cfg.state_dir / config.CONFIG_FILE
    path.write_text(path.read_text().replace('# check = "uv run pytest"', 'check = "uv run pytest"'))
    loaded = config.load(cfg.root)
    assert loaded.git.check == ""  # guarded edits wait for the human
    from troupe.store import Store
    store = Store(cfg.db_path)
    store.answer(store.kv_get("safety.config")["qid"], "Approve")
    assert config.load(cfg.root).git.check == "uv run pytest"
    assert loaded.git.check_timeout == 600
    with pytest.raises(ValueError, match="git.check_timeout"):
        config.GitSettings(check_timeout=-1)
