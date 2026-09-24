import asyncio

import pytest

from troupe.api import APIError, APIServer
from troupe.api_client import Client
from troupe.config import ProviderCfg
from troupe.engine import Engine, Wake
from troupe.store import Store
from troupe.team import TeamAPI


def test_additive_migration_and_milestone_tools(project):
    cfg, s = project
    # Simulate the old tasks schema on an existing populated database.
    s.conn.execute("ALTER TABLE tasks DROP COLUMN milestone_id")
    tid = s.x("INSERT INTO tasks(title,status,depends_on) VALUES('legacy','done','[]')")
    reopened = Store(cfg.db_path)
    assert (
        reopened.task(tid)["title"] == "legacy"
        and reopened.task(tid)["milestone_id"] is None
    )
    lead = TeamAPI(cfg, reopened, "lead")
    assert "Milestone #1" in lead.milestone(
        "create", name="Ship", goal="A working project", order=2
    )
    assert lead.milestone("update", 1, status="done").startswith("Milestone")
    for agent in ("pm", "builder-1", "qa"):
        assert (
            TeamAPI(cfg, reopened, agent)
            .milestone("create", name="bad")
            .startswith("ERROR:")
        )
    assert "ERROR:" in lead.milestone("update", 999, name="missing")
    assert "ERROR:" in lead.create_task("bad", "", milestone=999)
    assert "ERROR:" in TeamAPI(cfg, reopened, "pm").create_task("bad", "", milestone=1)
    assert "Created" in lead.create_task("Build", "", milestone=1)
    assigned = reopened.tasks()[-1]
    assert lead.update_task(tid, milestone=1).startswith("Task")
    cancelled = reopened.add_task("cancelled", status="cancelled", milestone_id=1)
    m = reopened.milestone(1)
    assert (m["done"], m["total"]) == (1, 2)
    assert "milestone #1 Ship" in lead.list_tasks(status="all", milestone=1)
    assert len(reopened.tasks(milestone_id=1)) == 3
    lead.update_task(tid, milestone=0)
    assert reopened.task(tid)["milestone_id"] is None
    assert "milestone" in {f.__name__ for f in lead.tools()}


def test_prompts_show_active_progress(project):
    cfg, s = project
    mid = s.add_milestone("Ready", "Ship it")
    s.add_task("done", status="done", milestone_id=mid)
    engine = Engine(cfg)
    for aid in ("lead", "pm"):
        a = cfg.agent(aid)
        assert "Ready: 1/1 done" in engine.build_prompt(
            a, Wake(1, a, "poke"), [], None, {}
        )
    s.update_milestone(mid, status="done")
    assert "Active milestones" not in engine.build_prompt(
        cfg.agent("lead"), Wake(1, cfg.agent("lead"), "poke"), [], None, {}
    )


def test_wait_precedence_and_stable_since(project, monkeypatch):
    cfg, s = project
    engine = Engine(cfg)
    agent = cfg.agent("builder-1")
    agent.providers = [ProviderCfg("claude"), ProviderCfg("codex")]
    monkeypatch.setattr("troupe.engine.now", lambda: 1000)
    task = s.add_task("mine", status="blocked", assignee=agent.id)
    s.task_note(task, "lead", "Need an answer")
    dep = s.add_task("dependency", status="ready")
    s.update_task(task, depends_on=[dep])
    s.kv_set("limit.claude", 1100)
    s.kv_set("limit.codex", 1200)
    q = s.ask(agent.id, "What next?")

    def wait():
        return engine.publish_wait_states()[agent.id]["waiting_on"]

    assert wait()["kind"] == "human" and wait()["targets"] == ["human"]
    monkeypatch.setattr("troupe.engine.now", lambda: 1005)
    assert wait()["since"] == 1000
    s.answer(q, "go")
    s.update_task(task, status="review", reviewer="qa")
    assert wait()["kind"] == "review" and wait()["targets"] == ["qa_1@test-project"]
    s.update_task(task, status="blocked")
    assert wait()["kind"] == "dependency" and wait()["targets"] == [dep]
    s.update_task(dep, status="done")
    assert wait()["kind"] == "blocked" and wait()["detail"] == "Need an answer"
    s.update_task(task, status="in_progress")
    assert wait()["kind"] == "providers" and wait()["reset_at"] == 1100
    s.kv_set("limit.codex", 0)
    assert wait()["kind"] == "rate_limit" and wait()["reset_at"] == 1100
    s.kv_set("limit.claude", 0)
    assert wait()["kind"] == "parked"
    assert s.wait_states()[agent.id]["waiting_on"]["kind"] == "parked"
    s.set_agent(agent.id, state="running")
    assert wait() is None


def test_mail_and_slot_snapshots(project):
    cfg, s = project
    engine = Engine(cfg)
    cfg.budget.max_concurrent = 1
    a, b = cfg.agent("builder-1"), cfg.agent("builder-2")
    s.send("lead", a.id, "queued")
    s.send("lead", a.id, "also queued")
    engine.running[b.id] = (None, None, Wake(3, b, "task"))
    states = engine.publish_wait_states([Wake(2, a, "messages")])
    assert states[a.id]["mail_queued"] == 2
    assert states[a.id]["waiting_on"]["kind"] == "slot"
    assert states[a.id]["waiting_on"]["queue_position"] == 1
    s.mark_read([m["id"] for m in s.unread(a.id)])
    s.set_agent(a.id, state="running")
    s.kv_set("mail_reading." + a.id, 2)
    states = engine.publish_wait_states()
    assert states[a.id] == dict(waiting_on=None, mail_queued=0, mail_reading=2)
    s.set_agent(a.id, state="idle")
    assert engine.publish_wait_states()[a.id]["mail_reading"] == 0


def test_milestone_api_and_events(project):
    cfg, s = project
    server = APIServer(cfg).start()
    try:
        with Client(cfg.root) as c:
            c.call("subscribe", dict(topics=["milestone.changed"]))
            m = c.call(
                "milestone",
                dict(action="create", name="Release", goal="Useful", order=1),
            )["milestone"]
            assert c.event()["data"]["milestone"]["id"] == m["id"]
            t = c.call("create_task", dict(title="one", milestone_id=m["id"]))["task"]
            assert t["milestone_id"] == m["id"]
            assert c.event()["data"]["milestone"]["total"] == 1
            c.call("update_task", dict(id=t["id"], fields=dict(status="done")))
            assert c.event()["data"]["milestone"]["done"] == 1
            assert (
                c.call("tasks", dict(milestone_id=m["id"]))["items"][0]["id"] == t["id"]
            )
            assert c.call("snapshot")["milestones"][0]["done"] == 1
            c.call("milestone", dict(action="update", id=m["id"], status="done"))
            assert c.call("milestones")["items"][0]["status"] == "done"
            with pytest.raises(APIError) as err:
                c.call("update_task", dict(id=t["id"], fields=dict(milestone_id=999)))
            assert err.value.code == "not_found"
            s.ask("builder-1", "answer")
            Engine(cfg).publish_wait_states()
            a = next(a for a in c.call("agents")["items"] if a["id"] == "builder-1")
            assert a["waiting_on"]["kind"] == "human"
    finally:
        server.stop()


def test_mcp_exposes_milestone_and_assignment_parameters(project):
    from troupe.mcp_server import build_server

    cfg, _ = project
    server, _ = build_server(cfg.root, "lead")
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert "milestone" in tools
    for name in ("create_task", "update_task", "list_tasks"):
        assert "milestone" in tools[name].input_schema["properties"]


def test_tick_publishes_without_reader_recomputation(project):
    cfg, s = project
    engine = Engine(cfg)
    s.kv_set("paused", True)
    s.ask("builder-1", "Help?")
    s.send("lead", "builder-1", "FYI")
    asyncio.run(engine.tick())
    snapshot = s.wait_states()["builder-1"]
    assert snapshot["waiting_on"]["kind"] == "human"
    assert snapshot["mail_queued"] == 1


def test_task_approval_human_precedence_and_same_kind_target_change(
    project, monkeypatch
):
    cfg, s = project
    engine = Engine(cfg)
    task = s.add_task("protected", status="review", assignee="builder-1")
    s.update_task(task, reviewer="qa")
    s.ask("system", "Approve?", kind="approval", task_id=task)
    monkeypatch.setattr("troupe.engine.now", lambda: 100)
    assert engine.publish_wait_states()["builder-1"]["waiting_on"]["kind"] == "human"
    s.x("UPDATE questions SET status='answered'")
    monkeypatch.setattr("troupe.engine.now", lambda: 200)
    first = engine.publish_wait_states()["builder-1"]["waiting_on"]
    assert first["kind"] == "review" and first["since"] == 200
    s.update_task(task, reviewer="lead")
    monkeypatch.setattr("troupe.engine.now", lambda: 300)
    second = engine.publish_wait_states()["builder-1"]["waiting_on"]
    assert second["since"] == 200 and second["targets"] == ["lead_1@test-project"]


def test_launch_captures_delivered_mail_and_clears_reading_after_run(project, monkeypatch):
    from troupe.runners import Runner, RunResult
    cfg, s = project
    engine = Engine(cfg)
    a = cfg.agent('builder-1')
    s.send('lead', a.id, 'first')
    s.send('pm', a.id, 'second')
    async def exercise():
        finish = asyncio.Event()
        class WaitingRunner(Runner):
            async def run(self, spec, emit):
                await finish.wait()
                return RunResult(ok=True)
        monkeypatch.setattr('troupe.engine.make_runner', lambda _: WaitingRunner())
        await engine.launch(Wake(2, a, 'messages'))
        task = engine.running[a.id][1]
        current = engine.publish_wait_states()[a.id]
        assert current == dict(waiting_on=None, mail_queued=0, mail_reading=2)
        finish.set()
        await task
        assert engine.publish_wait_states()[a.id]['mail_reading'] == 0
    asyncio.run(exercise())
