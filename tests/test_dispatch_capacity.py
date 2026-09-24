"""Capacity and fairness regressions with real candidate selection, no provider calls."""
import asyncio

import pytest

from troupe.engine import Engine, Wake


@pytest.mark.parametrize('status', ['ready', 'in_progress', 'blocked'])
def test_single_builder_cap_includes_existing_work(project, status):
    cfg, store = project
    cfg.agents = [a for a in cfg.agents if a.id != 'builder-2']
    store.sync_agents(cfg.agents)
    store.add_task('Existing', assignee='builder-1', status=status)
    tid = store.add_task('Next', status='ready')
    Engine(cfg).dispatch()
    assert store.task(tid)['assignee'] is None


def test_single_builder_gets_only_one_of_two_ready_tasks(project):
    cfg, store = project
    cfg.agents = [a for a in cfg.agents if a.id != 'builder-2']
    store.sync_agents(cfg.agents)
    for title in ('First', 'Second'):
        store.add_task(title, status='ready')
    Engine(cfg).dispatch()
    assert sum(t['assignee'] == 'builder-1' for t in store.tasks()) == 1


@pytest.mark.parametrize('status', ['backlog', 'review', 'approved', 'done', 'cancelled'])
def test_inactive_assignments_do_not_block_dispatch(project, status):
    cfg, store = project
    cfg.agents = [a for a in cfg.agents if a.id != 'builder-2']
    store.sync_agents(cfg.agents)
    store.add_task('Old', assignee='builder-1', status=status)
    tid = store.add_task('New', status='ready')
    Engine(cfg).dispatch()
    assert store.task(tid)['assignee'] == 'builder-1'


def test_idle_builder_gets_work_while_other_blocked(project):
    cfg, store = project
    store.add_task('Blocked', assignee='builder-1', status='blocked')
    tid = store.add_task('Next', status='ready')
    Engine(cfg).dispatch()
    assert store.task(tid)['assignee'] == 'builder-2'


def setup_scheduler(project, monkeypatch, slots=1):
    cfg, store = project
    cfg.budget.max_concurrent = slots
    engine = Engine(cfg)
    monkeypatch.setattr(engine, 'process_approved', lambda: None)
    monkeypatch.setattr('troupe.engine.now', lambda: 1000)
    launched = []
    async def launch(w):
        launched.append(w)
        engine.running[w.agent.id] = (None, None, w)
    monkeypatch.setattr(engine, 'launch', launch)
    for aid in ('lead', 'pm', 'spec'):
        store.send('qa', aid, 'Coordination')
    store.x('UPDATE messages SET ts=990')
    return engine, launched


@pytest.mark.parametrize('builder_mail', [False, True])
def test_work_wins_next_free_slot_under_continuous_mail(project, monkeypatch, builder_mail):
    cfg, store = project
    engine, launched = setup_scheduler(project, monkeypatch)
    store.add_task('Build', status='ready', assignee='builder-1')
    if builder_mail:
        store.send('lead', 'builder-1', 'Context')
        store.x('UPDATE messages SET ts=990')
    engine.running['pm'] = (None, None, Wake(2, cfg.agent('pm'), 'messages'))
    async def scenario():
        await engine.tick()
        assert not launched  # no preemption / oversubscription
        engine.running.clear()
        await engine.tick()
        assert len(launched) == 1 and launched[0].agent.id == 'builder-1'
        assert launched[0].reason == ('messages' if builder_mail else 'task')
        await engine._merge_task
    asyncio.run(scenario())


def test_remaining_slots_keep_priority_and_chat_stays_first(project, monkeypatch):
    cfg, store = project
    engine, launched = setup_scheduler(project, monkeypatch, slots=2)
    store.add_task('Build', status='ready', assignee='builder-1')
    store.send('human', 'pm', 'Hello', kind='chat')
    store.x('UPDATE messages SET ts=990')
    async def scenario():
        await engine.tick()
        assert [w.reason for w in launched] == ['chat', 'task', 'messages']
        assert sum(not w.chat for w in launched) == 2
        await engine._merge_task
    asyncio.run(scenario())


@pytest.mark.parametrize('gate', ['paused', 'budget', 'backend', 'backoff', 'task_delay'])
def test_reservation_never_bypasses_gates(project, monkeypatch, gate):
    cfg, store = project
    engine, launched = setup_scheduler(project, monkeypatch)
    tid = store.add_task('Build', status='ready', assignee='builder-1')
    if gate == 'paused':
        store.kv_set('paused', True)
    elif gate == 'budget':
        monkeypatch.setattr(engine, 'budget_ok', lambda: False)
    elif gate == 'backend':
        store.kv_set('limit.claude', 2000)
    elif gate == 'backoff':
        engine.failures['builder-1'] = (1, 2000)
    else:
        store.update_task(tid, next_attempt_at=2000)
    async def scenario():
        await engine.tick()
        assert not any(w.agent.id == 'builder-1' for w in launched)
        await engine._merge_task
    asyncio.run(scenario())


def test_active_task_run_leaves_other_slots_to_coordination(project, monkeypatch):
    cfg, store = project
    engine, launched = setup_scheduler(project, monkeypatch, slots=2)
    tid = store.add_task('Active', status='in_progress', assignee='builder-1')
    store.add_task('Waiting', status='ready', assignee='builder-2')
    engine.running['builder-1'] = (None, None, Wake(3, cfg.agent('builder-1'), 'task', store.task(tid)))
    async def scenario():
        await engine.tick()
        assert len(launched) == 1 and launched[0].reason == 'messages'
        await engine._merge_task
    asyncio.run(scenario())


def test_reserved_slot_prefers_least_recently_run_worker(project, monkeypatch):
    cfg, store = project
    engine, launched = setup_scheduler(project, monkeypatch)
    for aid, last in [('builder-1', 990), ('builder-2', 900)]:
        store.add_task(aid, status='ready', assignee=aid)
        store.set_agent(aid, last_run_at=last)
    async def scenario():
        await engine.tick()
        assert launched[0].agent.id == 'builder-2'
        await engine._merge_task
    asyncio.run(scenario())
