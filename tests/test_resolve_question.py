"""Chat decisions close inbox cards without manufacturing a second reply."""
import asyncio
import sqlite3

import pytest

from troupe.engine import Engine, Wake
from troupe.gui.data import Data
from troupe.mcp_server import build_server
from troupe.roles import CHARTER
from troupe.store import SCHEMA, Store
from troupe.team import TeamAPI


def test_chat_answer_closes_card_and_is_recalled_without_mail(project):
    """#65: only the PM asks the human directly (everyone else escalates), so this exercises the
    PM's own direct question, not a routed one."""
    cfg, store = project
    api = TeamAPI(cfg, store, "pm")
    data = Data(cfg)
    data.refresh(force=True)
    assert "inbox" in api.ask_human("Choose storage?", options=["SQLite", "Postgres"])
    qid = store.questions()[0]["id"]
    data.refresh(force=True)
    assert [q["id"] for q in data.questions] == [qid]
    store.send("human", "pm", "SQLite, please", kind="chat")
    before = len(store.messages())
    assert not api.resolve_question(qid, "SQLite, please").startswith("ERROR:")
    assert len(store.messages()) == before
    row = store.questions(status="answered")[0]
    assert row["answer"] == "SQLite, please"
    assert row["answered_via"] == "chat"
    assert row["answered_at"] is not None
    assert "SQLite, please" in api.recall("storage")
    data.refresh(force=True)
    assert data.questions == []
    assert [q["id"] for q in data.new_chat_answers] == [qid]
    data.new_chat_answers = []
    data.refresh(force=True)
    assert data.new_chat_answers == []
    reopened = Data(cfg)
    reopened.refresh(force=True)
    assert reopened.new_chat_answers == []


@pytest.mark.parametrize("resolver,allowed", [("builder-1", True), ("builder-2", False),
                                               ("lead", True), ("pm", True), ("qa", False)])
def test_resolution_permissions_and_closed_question(project, resolver, allowed):
    cfg, store = project
    qid = store.ask("builder-1", "Choose storage?")
    api = TeamAPI(cfg, store, resolver)
    result = api.resolve_question(qid, "SQLite")
    assert result.startswith("ERROR:") is not allowed
    if allowed:
        assert api.resolve_question(qid, "overwrite").startswith("ERROR:")
        assert store.questions("answered")[0]["answer"] == "SQLite"
    else:
        assert len(store.questions()) == 1
    assert not store.messages()


def test_validation_and_inbox_answer_remain_compatible(project):
    cfg, store = project
    api = TeamAPI(cfg, store, "pm")
    qid = store.ask("pm", "Pick one")
    for answer, via in [("", "chat"), ("  ", "chat"), ("yes", "email")]:
        assert api.resolve_question(qid, answer, via).startswith("ERROR:")
    assert api.resolve_question(9999, "yes").startswith("ERROR:")
    assert store.answer(qid, "yes")
    assert store.questions("answered")[0]["answered_via"] == "inbox"
    assert len(store.unread("pm")) == 1
    assert not store.answer(qid, "no")
    assert len(store.unread("pm")) == 1


def test_old_database_migration(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO questions(asker,question,status,answer) VALUES('pm','Old?', 'answered','yes')")
    store = Store(path)
    row = store.questions("answered")[0]
    assert row["answer"] == "yes"
    assert row["answered_via"] == "inbox"
    assert Store(path).questions("answered")[0] == row


def test_chat_prompt_includes_only_own_open_questions(project):
    cfg, store = project
    a = cfg.agent("builder-1")
    own = store.ask(a.id, "Which database?")
    store.ask("pm", "Unrelated PM question")
    closed = store.ask(a.id, "Already decided")
    store.answer(closed, "done")
    engine = Engine(cfg)
    prompt = engine.build_prompt(a, Wake(0, a, "chat"), [], None, {})
    assert "Your open questions (did the human just answer one? if so, resolve_question)" in prompt
    assert f"#{own}: Which database?" in prompt
    assert "Unrelated PM question" not in prompt
    assert "Already decided" not in prompt
    assert "Check your open questions against what the human just said" in prompt
    assert prompt.endswith("Principle 0 applies: the human comes first.")
    assert "ALSO file it with ask_human (with options)" in CHARTER


def test_mcp_tool_is_registered(project):
    cfg, _ = project
    server, api = build_server(cfg.root, "pm")
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "resolve_question")
    assert "question_id" in tool.input_schema["properties"]
    assert api.resolve_question in api.tools()


def test_dismissed_question_and_explicit_inbox_resolution(project):
    cfg, store = project
    api = TeamAPI(cfg, store, "pm")
    dismissed = store.ask("pm", "Ignore?")
    store.answer(dismissed, "use judgment", status="dismissed")
    assert api.resolve_question(dismissed, "replace").startswith("ERROR:")
    qid = store.ask("pm", "Another?")
    before = len(store.messages())
    assert not api.resolve_question(qid, "yes", via="inbox").startswith("ERROR:")
    assert len(store.messages()) == before
    assert store.one("SELECT answered_via FROM questions WHERE id=?", qid)["answered_via"] == "inbox"


def test_concurrent_answers_only_resolve_once(project):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _, store = project
    qid = store.ask("pm", "Race?")
    barrier = Barrier(2)
    original_one = store.one
    def synchronized_one(sql, *args):
        result = original_one(sql, *args)
        barrier.wait(timeout=5)
        return result
    store.one = synchronized_one
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda answer: store.answer(qid, answer), ["first", "second"]))
    assert sorted(results) == [False, True]
    assert len(store.unread("pm")) == 1
    assert len([e for e in store.events() if e["kind"] == "answer"]) == 1
