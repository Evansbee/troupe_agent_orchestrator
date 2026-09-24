"""Setup and cleanup exercised against actual temporary git repositories."""
import asyncio
import json
from pathlib import Path

import pytest

from troupe import config, gitops
from troupe.engine import Engine, Wake
from troupe.runners import Runner, RunResult


class CaptureRunner(Runner):
    def __init__(self):
        super().__init__()
        self.specs = []

    async def run(self, spec, emit):
        self.specs.append(spec)
        return RunResult(ok=True)


async def run_task(engine, agent, task):
    await engine.launch(Wake(3, agent, "task", task))
    await engine.running[agent.id][1]


def test_setup_config_default_and_explicit(project):
    cfg, _ = project
    assert cfg.git.setup == ""
    path = cfg.state_dir / 'troupe.toml'
    path.write_text(path.read_text().replace('setup = ""', 'setup = "uv sync"'))
    assert config.load(cfg.root).git.setup == "uv sync"
    path.write_text(path.read_text().replace('setup = "uv sync"', 'check = "pytest"'))
    assert config.load(cfg.root).git.setup == ""


@pytest.mark.parametrize("code", [0, 7])
def test_setup_once_output_and_failure_prompt(project, monkeypatch, code):
    cfg, store = project
    cfg.git.setup = f"echo setup-out; echo setup-err >&2; echo ran >> setup-count; exit {code}"
    runner = CaptureRunner()
    monkeypatch.setattr('troupe.engine.make_runner', lambda _: runner)
    tid = store.add_task('Set up', status='ready', assignee='builder-1')
    agent = cfg.agent('builder-1')
    engine = Engine(cfg)
    asyncio.run(run_task(engine, agent, store.task(tid)))
    task = store.task(tid)
    path = Path(task['worktree'])
    assert (path / 'setup-count').read_text() == 'ran\n'
    log = runner.specs[0].log_path.read_text()
    assert 'setup-out' in log and 'setup-err' in log
    assert json.loads(log.splitlines()[-1]) == {'setup_exit': code}
    if code:
        assert 'exit 7' in store.task_notes(tid)[0]['text']
        assert 'setup-err' in runner.specs[0].prompt
    else:
        assert store.task_notes(tid) == []
    # Restarting the engine and revisiting the same worktree must not run it again.
    engine = Engine(cfg)
    engine.recover()
    asyncio.run(run_task(engine, agent, store.task(tid)))
    assert len(runner.specs) == 2
    assert (path / 'setup-count').read_text() == 'ran\n'


def test_setup_does_not_block_event_loop(project, monkeypatch):
    cfg, store = project
    cfg.git.setup = 'sleep 0.2; echo ready > prepared'
    runner = CaptureRunner()
    monkeypatch.setattr('troupe.engine.make_runner', lambda _: runner)
    tid = store.add_task('Async setup', status='ready', assignee='builder-1')
    agent = cfg.agent('builder-1')
    engine = Engine(cfg)
    async def check():
        await engine.launch(Wake(3, agent, 'task', store.task(tid)))
        await asyncio.sleep(0.03)
        assert runner.specs == []
        assert agent.id in engine.running
        await engine.running[agent.id][1]
        assert (runner.specs[0].cwd / 'prepared').exists()
    asyncio.run(check())


def test_merge_removes_worktree_and_branch(project):
    cfg, store = project
    engine = Engine(cfg)
    tid = store.add_task('Merge cleanup', status='ready', assignee='builder-1')
    task = engine.start_task(cfg.agent('builder-1'), store.task(tid))
    path = Path(task['worktree'])
    (path / 'new.txt').write_text('merge me')
    gitops.commit_all(path, 'Task change')
    store.update_task(tid, status='approved')
    engine.process_approved()
    assert store.task(tid)['status'] == 'done'
    assert (cfg.root / 'new.txt').read_text() == 'merge me'
    assert not path.exists()
    assert task['branch'] not in gitops.git(cfg.root, 'branch', '--list').split()


def test_startup_removes_only_closed_or_missing_tasks(project):
    cfg, store = project
    engine = Engine(cfg)
    paths = {}
    for status in ('done', 'cancelled', 'ready', 'in_progress', 'blocked', 'review', 'approved'):
        tid = store.add_task(status, status=status)
        branch, path = gitops.create_worktree(cfg.root, cfg.worktrees_dir, tid, status)
        store.update_task(tid, branch=branch, worktree=str(path))
        paths[status] = path
    _, orphan = gitops.create_worktree(cfg.root, cfg.worktrees_dir, 999, 'orphan')
    _, external = gitops.create_worktree(cfg.root, cfg.root / 'outside', 998, 'external')
    engine.recover()
    assert not orphan.exists()
    assert external.exists()
    for status, path in paths.items():
        assert path.exists() == (status not in ('done', 'cancelled'))
    registered = gitops.git(cfg.root, 'worktree', 'list', '--porcelain')
    assert str(orphan) not in registered
    assert str(paths['done']) not in registered


def test_stop_during_setup_does_not_start_provider(project, monkeypatch):
    cfg, store = project
    cfg.git.setup = 'echo started; sleep 30'
    runner = CaptureRunner()
    monkeypatch.setattr('troupe.engine.make_runner', lambda _: runner)
    tid = store.add_task('Stop setup', status='ready', assignee='builder-1')
    agent = cfg.agent('builder-1')
    engine = Engine(cfg)
    async def stop():
        await engine.launch(Wake(3, agent, 'task', store.task(tid)))
        running = engine.running[agent.id][1]
        # #71: a real subprocess spawn (cfg.git.setup) races OS scheduling under load, so poll
        # generously (5s) rather than the original tight 1s budget.
        for _ in range(500):
            if runner.proc:
                break
            await asyncio.sleep(0.01)
        assert runner.proc
        runner.kill()
        await asyncio.wait_for(running, timeout=10)
    asyncio.run(stop())
    assert runner.specs == []
    assert store.runs()[0]['status'] == 'stopped'
