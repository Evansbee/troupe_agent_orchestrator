from troupe.engine import Engine
from troupe.team import TeamAPI


def test_lead_tasks_are_ready_others_backlog(project):
    cfg, store = project
    assert "[ready]" in TeamAPI(cfg, store, "lead").create_task("A", "do a")
    assert "[backlog]" in TeamAPI(cfg, store, "gadfly").create_task("B", "do b")


def test_message_fanout_by_role(project):
    cfg, store = project
    TeamAPI(cfg, store, "lead").send_message("builder", "hello")
    assert len(store.unread("builder-1")) == 1
    assert len(store.unread("builder-2")) == 1
    assert store.unread("lead") == []


def test_question_limit_and_answer_delivery(project):
    """#65: only the PM asks the human directly (everyone else escalates), so the open-questions
    cap only bites the PM now."""
    cfg, store = project
    api = TeamAPI(cfg, store, "pm")
    for i in range(4):
        api.ask_human(f"q{i}?", options=["yes", "no"])
    assert api.ask_human("one too many?").startswith("ERROR")
    qid = store.questions()[0]["id"]
    store.answer(qid, "yes")
    mail = store.unread("pm")
    assert mail and "yes" in mail[0]["body"]


def test_only_lead_changes_priority(project):
    cfg, store = project
    TeamAPI(cfg, store, "lead").create_task("A", "do a")
    assert TeamAPI(cfg, store, "qa").update_task(1, priority=0).startswith("ERROR")
    assert not TeamAPI(cfg, store, "lead").update_task(1, priority=0).startswith("ERROR")


def test_dispatch_assigns_one_task_per_builder(project):
    cfg, store = project
    lead = TeamAPI(cfg, store, "lead")
    for name in ("A", "B", "C"):
        lead.create_task(name, "x")
    Engine(cfg).dispatch()
    assignees = [t["assignee"] for t in store.tasks()]
    assert sorted(a for a in assignees if a) == ["builder-1", "builder-2"]


def test_human_chat_preempts_other_wakes(project):
    cfg, store = project
    store.send("human", "pm", "hi", kind="chat")
    wakes = Engine(cfg).candidates(paused=False)
    assert all(w.reason != "proactive" or w.agent.id != "pm" for w in wakes)


def test_recall_finds_decisions(project):
    cfg, store = project
    TeamAPI(cfg, store, "spec").remember("Use SQLite", rationale="local-first")
    assert "Use SQLite" in TeamAPI(cfg, store, "lead").recall("sqlite")
