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


def test_dirty_tree_after_check_retries_once_then_bounces_clearly(project, monkeypatch):
    """The check command itself leaves the tree dirty (a check-script bug, not main moving): the
    first "repository changed" is retried (#81) same as any other, but the retry's own prepare_check
    finds the tree still dirty and can't proceed automatically — that's not the same as main moving
    again, so it bounces right away instead of burning the rest of the retry budget."""
    from troupe import gates as gates_module
    monkeypatch.setattr(gates_module, "MERGE_RETRY_BACKOFF_SECONDS", 0)
    cfg, store, engine, tid, tree = approved(project)
    cfg.git.check = "echo unchecked > feature.txt"
    engine.process_approved()
    task = store.task(tid)
    assert task["status"] == "in_progress"
    assert "uncommitted changes" in task["review_notes"]
    assert not (cfg.root / "feature.txt").exists()


def test_main_moving_during_check_retries_and_merges_without_bouncing(project, monkeypatch):
    """#81: main moves constantly from non-builder autocommits — that shouldn't cost the builder a
    wake. The worker re-merges main into the task tree and re-runs the check itself.

    "moved.txt" isn't under any of #107's doc_only_paths globs, so this stays a real retry (today's
    #81 behavior) rather than the #107 first-attempt-succeeds path below — this pins that down."""
    from troupe import gates as gates_module
    monkeypatch.setattr(gates_module, "MERGE_RETRY_BACKOFF_SECONDS", 0)
    cfg, store, engine, tid, tree = approved(project)
    real_run_check = gitops.run_check
    moved = {"done": False}
    def run_check(tree_, command, timeout, log_path, stop):
        if not moved["done"]:
            moved["done"] = True
            (cfg.root / "moved.txt").write_text("moved mid-check")
            gitops.commit_all(cfg.root, "Main advanced mid-check")
        return real_run_check(tree_, command, timeout, log_path, stop)
    monkeypatch.setattr(gitops, "run_check", run_check)
    cfg.git.check = "true"
    engine.process_approved()
    task = store.task(tid)
    assert task["status"] == "done"
    assert moved["done"]
    assert (cfg.root / "feature.txt").exists() and (cfg.root / "moved.txt").exists()
    bodies = [m["body"] for m in store.messages() if m["recipient"] == "builder-1"]
    assert not any("changed" in b.lower() or "failed" in b.lower() for b in bodies)
    assert any("merged into main" in b for b in bodies)
    assert store.kv_get(f"check_failures.{tid}", 0) == 0


def test_doc_only_main_movement_merges_on_first_attempt(project, monkeypatch):
    """#107: a specs/**-only autocommit to main during the check can't affect what the check just
    verified, so it doesn't count as "repository changed" — the merge lands on the very first
    attempt (no retry needed), and the merged tree includes both the task's own change and the doc
    commit that landed on main while the check was running."""
    cfg, store, engine, tid, tree = approved(project)
    real_run_check = gitops.run_check
    calls = []
    def run_check(tree_, command, timeout, log_path, stop):
        calls.append(1)
        (cfg.root / "specs").mkdir(exist_ok=True)
        (cfg.root / "specs" / "99-scratch.md").write_text("doc change mid-check")
        gitops.commit_all(cfg.root, "troupe(spec): doc-only change mid-check")
        return real_run_check(tree_, command, timeout, log_path, stop)
    monkeypatch.setattr(gitops, "run_check", run_check)
    cfg.git.check = "true"
    engine.process_approved()
    assert len(calls) == 1  # first attempt succeeded — no retry needed
    task = store.task(tid)
    assert task["status"] == "done"
    assert (cfg.root / "feature.txt").exists()  # the task's own change
    assert (cfg.root / "main.txt").exists()  # main's pre-check content
    assert (cfg.root / "specs" / "99-scratch.md").exists()  # the doc commit, not reverted
    bodies = [m["body"] for m in store.messages() if m["recipient"] == "builder-1"]
    assert any("merged into main" in b for b in bodies)


def test_non_doc_path_alongside_a_doc_path_still_retries(project, monkeypatch):
    """#107: the doc-only exception is all-or-nothing per commit set since main_head — a mid-check
    autocommit that touches even one path outside doc_only_paths must still count as "repository
    changed" and go through the normal retry, not be waved through because most of it was docs."""
    from troupe import gates as gates_module
    monkeypatch.setattr(gates_module, "MERGE_RETRY_BACKOFF_SECONDS", 0)
    cfg, store, engine, tid, tree = approved(project)
    real_run_check = gitops.run_check
    calls = []
    moved = {"done": False}
    def run_check(tree_, command, timeout, log_path, stop):
        calls.append(1)
        if not moved["done"]:
            moved["done"] = True
            (cfg.root / "specs").mkdir(exist_ok=True)
            (cfg.root / "specs" / "99-scratch.md").write_text("doc change mid-check")
            (cfg.root / "src_change.py").write_text("not a doc path")
            gitops.commit_all(cfg.root, "Mixed doc + code change mid-check")
        return real_run_check(tree_, command, timeout, log_path, stop)
    monkeypatch.setattr(gitops, "run_check", run_check)
    cfg.git.check = "true"
    engine.process_approved()
    task = store.task(tid)
    assert task["status"] == "done"  # the retry still succeeds — this isn't testing exhaustion
    assert (cfg.root / "src_change.py").exists()
    assert len(calls) == 2  # first-attempt check ran, "changed" triggered a second — not waved through


def test_repository_keeps_changing_stays_approved_and_requeues_instead_of_bouncing(project, monkeypatch):
    """#107: after MERGE_RETRY_ATTEMPTS consecutive "repository changed" results from real code
    movement (not doc/spec-only, so the #107 doc-only exception doesn't apply), the task no longer
    bounces to the builder — an approved task didn't get worse because main moved, so it stays
    `approved` and is requeued with a backoff instead of costing a builder/QA cycle."""
    from troupe.gates import MERGE_RETRY_ATTEMPTS, MERGE_REQUEUE_BACKOFF_SECONDS
    from troupe import gates as gates_module
    from troupe.store import now
    monkeypatch.setattr(gates_module, "MERGE_RETRY_BACKOFF_SECONDS", 0)
    cfg, store, engine, tid, tree = approved(project)
    real_run_check = gitops.run_check
    calls = []
    def run_check(tree_, command, timeout, log_path, stop):
        calls.append(1)
        (cfg.root / f"moved{len(calls)}.txt").write_text("moved")  # not a doc-only path
        gitops.commit_all(cfg.root, f"Main advanced #{len(calls)}")
        return real_run_check(tree_, command, timeout, log_path, stop)
    monkeypatch.setattr(gitops, "run_check", run_check)
    cfg.git.check = "true"
    before = now()
    engine.process_approved()
    assert len(calls) == MERGE_RETRY_ATTEMPTS
    task = store.task(tid)
    assert task["status"] == "approved"  # not bounced to in_progress
    assert task["next_attempt_at"] >= before + MERGE_REQUEUE_BACKOFF_SECONDS
    assert not store.kv_get(f"check_failed.{tid}")
    assert not store.unread("builder-1")  # builder not woken
    assert not (cfg.root / "feature.txt").exists()  # nothing merged yet
    log = (cfg.state_dir / "checks" / f"t{tid}.log").read_text()
    assert "requeued" in log.lower() and str(MERGE_RETRY_ATTEMPTS) in log


def test_requeued_task_does_not_recheck_until_its_backoff_elapses(project, monkeypatch):
    """#107: the requeue backoff must actually stop the gate from immediately spending another
    full check on the same task — otherwise "requeue with a backoff" is just relabeled thrashing."""
    from troupe.gates import MERGE_RETRY_ATTEMPTS
    from troupe import gates as gates_module
    monkeypatch.setattr(gates_module, "MERGE_RETRY_BACKOFF_SECONDS", 0)
    cfg, store, engine, tid, tree = approved(project)
    real_run_check = gitops.run_check
    calls = []
    def run_check(tree_, command, timeout, log_path, stop):
        calls.append(1)
        (cfg.root / f"moved{len(calls)}.txt").write_text("moved")
        gitops.commit_all(cfg.root, f"Main advanced #{len(calls)}")
        return real_run_check(tree_, command, timeout, log_path, stop)
    monkeypatch.setattr(gitops, "run_check", run_check)
    cfg.git.check = "true"
    engine.process_approved()
    assert len(calls) == MERGE_RETRY_ATTEMPTS
    engine.process_approved()  # immediately again: still inside the backoff window
    assert len(calls) == MERGE_RETRY_ATTEMPTS  # no new checks ran
    store.update_task(tid, next_attempt_at=0)  # simulate the backoff having elapsed
    engine.process_approved()
    assert len(calls) > MERGE_RETRY_ATTEMPTS  # retried for real this time


def test_real_check_failure_bounces_immediately_without_retrying(project, monkeypatch):
    """#81: a real test failure is never retried — retrying the same code against the same check
    can't change the outcome, so it should bounce on the very first attempt."""
    cfg, store, engine, tid, tree = approved(project)
    calls = []
    real_run_check = gitops.run_check
    def run_check(*args, **kwargs):
        calls.append(1)
        return real_run_check(*args, **kwargs)
    monkeypatch.setattr(gitops, "run_check", run_check)
    cfg.git.check = "exit 3"
    engine.process_approved()
    assert len(calls) == 1
    assert store.task(tid)["status"] == "in_progress"


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


def test_doc_only_paths_config_default_and_validation():
    assert config.GitSettings().doc_only_paths == [
        "specs/**", "design/**", "docs/**", "*.md", "README*", "LICENSE"]
    with pytest.raises(ValueError, match="doc_only_paths"):
        config.GitSettings(doc_only_paths="specs/**")  # must be a list, not a bare string
    with pytest.raises(ValueError, match="doc_only_paths"):
        config.GitSettings(doc_only_paths=[1, 2])
