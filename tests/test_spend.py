"""REQ-ENG-070: spend accounting -- cost/tokens/rework computed from runs+events, no new columns."""
import time

from troupe import spend
from troupe.engine import Engine, Wake


def _run(store, agent, reason, task_id=None, cost=1.0, tokens=100, started=None, tool_call=True):
    started = time.time() if started is None else started
    rid = store.x(
        "INSERT INTO runs(agent,started,ended,reason,status,cost,tokens,task_id,cwd,chat) "
        "VALUES(?,?,?,?,?,?,?,?,?,0)",
        agent, started, started + 10, reason, "ok", cost, tokens, task_id, "/tmp",
    )
    if tool_call:
        store.run_line(rid, "tool", "edit foo.py")
    return rid


def test_qa_rejection_then_resubmit_is_rework(project):
    cfg, store = project
    tid = store.add_task("Widget", status="review", assignee="builder-1")
    t0 = time.time()
    first = _run(store, "builder-1", "task", task_id=tid, started=t0)
    store.event("qa_1", "task", f"qa_1 rejected #{tid} Widget", ref=f"task:{tid}")
    second = _run(store, "builder-1", "task", task_id=tid, started=t0 + 100)

    runs = {r.id: r for r in spend.load_runs(store)}
    assert runs[first].rework is False
    assert runs[second].rework is True


def test_qa_rereview_after_rejection_is_also_rework(project):
    """QA #120 rejection: rework isn't just the rebuild -- QA's RE-review of the resubmitted branch
    (reason='review', not 'task') is exactly the re-spend a bounce causes, and must count too."""
    cfg, store = project
    tid = store.add_task("Widget", status="review", assignee="builder-1")
    t0 = time.time()
    first_build = _run(store, "builder-1", "task", task_id=tid, started=t0)
    # Same clock, not a synthetic forward offset: store.event() below stamps the reject with the
    # *real* current time, which only ever advances -- a t0+N started value large enough to race
    # past that real clock would wrongly look like it started after the rejection.
    first_review = _run(store, "qa_1", "review", task_id=tid, started=t0)
    store.event("qa_1", "task", f"qa_1 rejected #{tid} Widget", ref=f"task:{tid}")
    rework_build = _run(store, "builder-1", "task", task_id=tid, started=t0 + 100)
    re_review = _run(store, "qa_1", "review", task_id=tid, started=t0 + 110)

    runs = {r.id: r for r in spend.load_runs(store)}
    assert runs[first_build].rework is False
    assert runs[first_review].rework is False
    assert runs[rework_build].rework is True
    assert runs[re_review].rework is True


def test_gate_bounce_then_resubmit_is_rework(project):
    cfg, store = project
    tid = store.add_task("Widget", status="approved", assignee="builder-1")
    t0 = time.time()
    first = _run(store, "builder-1", "task", task_id=tid, started=t0)
    store.event("system", "task", f"Checks failed on #{tid}", ref=f"task:{tid}")
    second = _run(store, "builder-1", "task", task_id=tid, started=t0 + 100)

    runs = {r.id: r for r in spend.load_runs(store)}
    assert runs[first].rework is False
    assert runs[second].rework is True


def test_merge_conflict_then_resubmit_is_rework(project):
    cfg, store = project
    tid = store.add_task("Widget", status="approved", assignee="builder-1")
    t0 = time.time()
    first = _run(store, "builder-1", "task", task_id=tid, started=t0)
    store.event("system", "task", f"Merge conflict on #{tid} — back to builder-1", ref=f"task:{tid}")
    second = _run(store, "builder-1", "task", task_id=tid, started=t0 + 100)

    runs = {r.id: r for r in spend.load_runs(store)}
    assert runs[first].rework is False
    assert runs[second].rework is True


def test_duplicate_delivery_messages_wake_is_rework(project):
    cfg, store = project
    productive = _run(store, "builder-1", "messages", tool_call=True)
    duplicate = _run(store, "builder-1", "messages", tool_call=False)

    runs = {r.id: r for r in spend.load_runs(store)}
    assert runs[productive].produced_nothing is False
    assert runs[productive].rework is False
    assert runs[duplicate].produced_nothing is True
    assert runs[duplicate].rework is True


def test_a_task_run_before_any_bounce_is_not_rework(project):
    """A task's very first run, or any run before it's ever bounced, must not be misclassified."""
    cfg, store = project
    tid = store.add_task("Widget", status="ready", assignee="builder-1")
    rid = _run(store, "builder-1", "task", task_id=tid)
    runs = {r.id: r for r in spend.load_runs(store)}
    assert runs[rid].rework is False


def test_totals_match_across_by_agent_and_by_reason(project):
    cfg, store = project
    tid1 = store.add_task("A", status="review", assignee="builder-1")
    tid2 = store.add_task("B", status="review", assignee="builder-2")
    _run(store, "builder-1", "task", task_id=tid1, cost=2.5, tokens=1000)
    _run(store, "builder-2", "task", task_id=tid2, cost=1.5, tokens=500)
    _run(store, "pm", "chat", cost=0.25, tokens=50)
    _run(store, "lead", "proactive", cost=0.1, tokens=20)

    runs = spend.load_runs(store)
    total = spend.summary(store, runs)

    by_agent_total = sum(r["cost"] for r in spend.by_agent(store, runs))
    by_reason_total = sum(r["cost"] for r in spend.by_reason(store, runs))
    by_day_total = sum(r["cost"] for r in spend.by_day(store, runs))
    assert by_agent_total == total["cost"]
    assert by_reason_total == total["cost"]
    assert by_day_total == total["cost"]
    # by_task only covers runs with a task_id -- a documented, narrower subset (chat/proactive
    # wakes have none) -- so it must match that subset's total, not the grand total.
    task_runs_cost = sum(r.cost for r in runs if r.task_id is not None)
    assert sum(r["cost"] for r in spend.by_task(store, runs)) == task_runs_cost
    assert total["cost"] == 2.5 + 1.5 + 0.25 + 0.1


def test_since_cutoff_filters_by_start_time(project):
    cfg, store = project
    old = _run(store, "builder-1", "chat", started=time.time() - 10 * 86400)
    recent = _run(store, "builder-1", "chat", started=time.time() - 3600)

    runs_24h = {r.id for r in spend.load_runs(store, since="24h")}
    runs_7d = {r.id for r in spend.load_runs(store, since="7d")}
    runs_all = {r.id for r in spend.load_runs(store, since="all")}
    assert runs_24h == {recent}
    assert runs_7d == {recent}
    assert runs_all == {old, recent}


def test_since_rejects_unknown_value(project):
    cfg, store = project
    try:
        spend.load_runs(store, since="lastweek")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_pm_proactive_prompt_includes_the_weekly_spend_rollup(project):
    """Acceptance criterion 6: the PM's proactive check-in prompt carries a 7-day spend rollup, so
    the PM can report it to the human unprompted rather than waiting to be asked."""
    cfg, store = project
    _run(store, "builder-1", "task", cost=3.5, tokens=400)
    engine = Engine(cfg)
    pm = cfg.agent("pm")
    prompt = engine.build_prompt(pm, Wake(1, pm, "proactive"), [], None, {})
    assert "Spend, last 7 days" in prompt
    assert "$3.50" in prompt


def test_spend_rollup_is_absent_for_non_pm_agents_and_non_proactive_wakes(project):
    cfg, store = project
    _run(store, "builder-1", "task", cost=3.5, tokens=400)
    engine = Engine(cfg)
    pm = cfg.agent("pm")
    builder = cfg.agent("builder-1")
    assert "Spend, last 7 days" not in engine.build_prompt(pm, Wake(1, pm, "poke"), [], None, {})
    assert "Spend, last 7 days" not in engine.build_prompt(
        builder, Wake(1, builder, "proactive"), [], None, {})
