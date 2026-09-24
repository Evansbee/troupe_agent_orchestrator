"""#57: the API's stop_now/resume/safety-approval commands wired to #42's shared safety paths
(safety.stop_now/resume, gates.py's answer-text verdict parsing), not a second implementation.
REQ-SAFE-010 (kill switch), REQ-SAFE-020 (approval cards), REQ-SAFE-040 (audit).
"""
import asyncio
import sys
import time

import pytest

from troupe.api import APIError, APIServer
from troupe.api_client import Client
from troupe.engine import Engine, Wake
from troupe.gates import verdict
from troupe.runners import Runner, RunResult
from troupe.team import TeamAPI


@pytest.fixture
def api(project):
    cfg, store = project
    server = APIServer(cfg).start()
    try:
        yield server, store
    finally:
        server.stop()


def client(api, **kwargs):
    return Client(api[0].cfg.root, **kwargs)


def error(c, method, params, code):
    with pytest.raises(APIError) as exc:
        c.call(method, params)
    assert exc.value.code == code


class WaitingRunner(Runner):
    """Ignores SIGTERM so the test can prove SIGKILL follows within the 2s budget."""

    async def run(self, spec, emit):
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
            start_new_session=True)
        await self.proc.wait()
        return RunResult(ok=False, error="terminated")


def test_stop_now_over_the_api_kills_runs_within_2s_and_enters_stopped_state(api, monkeypatch):
    server, s = api
    cfg = server.cfg
    monkeypatch.setattr("troupe.engine.make_runner", lambda _: WaitingRunner())
    engine = Engine(cfg)

    async def scenario():
        for aid in ("lead", "pm"):
            s.send("human", aid, "chat", kind="chat")
            await engine.launch(Wake(0, cfg.agent(aid), "chat"))
        running = list(engine.running.values())
        for _ in range(100):
            if all(r.proc for r, _, _ in running):
                break
            await asyncio.sleep(.01)
        await asyncio.sleep(.1)  # children install their SIGTERM-ignoring handler

        start = time.monotonic()
        with client(api) as c:
            result = c.call("stop_now")
        assert result == {"killed": 2}
        await engine.tick()
        await asyncio.wait_for(asyncio.gather(*(t for _, t, _ in running)), 1.5)
        assert time.monotonic() - start < 2

        assert all(r.proc.returncode is not None for r, _, _ in running)
        assert all(r["status"] == "interrupted" for r in s.runs())
        assert s.kv_get("stopped") and s.kv_get("paused")
        assert engine.candidates(False) == []  # chat included: nothing dispatches while stopped
        assert any(e["kind"] == "safety" for e in s.events(limit=20))

    asyncio.run(scenario())


def test_resume_over_the_api_clears_stopped_and_is_audited(api):
    server, s = api
    with client(api) as c:
        c.call("stop_now")
        assert s.kv_get("stopped") and s.kv_get("paused")
        result = c.call("resume")
        assert result["engine"]["stopped"] is False and result["engine"]["paused"] is False
    assert not s.kv_get("stopped") and not s.kv_get("paused")
    assert any("resumed" in e["text"].lower() and e["kind"] == "safety" for e in s.events(limit=20))


def test_plain_pause_does_not_touch_the_kill_switch(api):
    server, s = api
    with client(api) as c:
        assert c.call("pause")["engine"]["paused"] is True
        assert s.kv_get("stopped") in (None, False)
        assert c.call("resume")["engine"]["paused"] is False


def test_answer_question_approves_a_safety_card_through_gates_verdict(api):
    server, s = api
    qid = s.ask("system", "Approve protected changes?", options=["Approve", "Reject"], kind="safety")
    with client(api) as c:
        result = c.call("answer_question", dict(id=qid, decision="approve", text=""))
    assert result["question"]["status"] == "answered"
    q = s.one("SELECT * FROM questions WHERE id=?", qid)
    assert q["answer"] == "Approve"
    assert verdict(s, dict(qid=qid)) == "approve"
    assert any(e["kind"] == "safety" and "approve" in e["text"].lower() for e in s.events(limit=20))


def test_answer_question_rejects_a_safety_card_with_a_note(api):
    server, s = api
    qid = s.ask("system", "Approve protected changes?", options=["Approve", "Reject"], kind="safety")
    with client(api) as c:
        result = c.call("answer_question", dict(id=qid, decision="reject", text="Too risky"))
    assert result["question"]["answer"] == "Reject — Too risky"
    assert verdict(s, dict(qid=qid)) == "reject"


def test_approval_kind_question_also_wired_the_same_way(api):
    """`kind="approval"` (the API's own display label) hits the same code path as the stored `safety`
    kind — both are gated the same way in command()."""
    server, s = api
    qid = s.ask("system", "Approve?", options=["Approve", "Reject"], kind="approval")
    with client(api) as c:
        c.call("answer_question", dict(id=qid, decision="approve", text=""))
    assert s.one("SELECT * FROM questions WHERE id=?", qid)["answer"] == "Approve"


def test_safety_card_cannot_be_dismissed_or_answered_without_a_decision(api):
    server, s = api
    qid = s.ask("system", "Approve?", options=["Approve", "Reject"], kind="safety")
    with client(api) as c:
        error(c, "dismiss_question", dict(id=qid), "forbidden")
        error(c, "answer_question", dict(id=qid, text="sure"), "bad_request")
        error(c, "answer_question", dict(id=qid, decision="maybe", text=""), "bad_request")
    assert s.one("SELECT * FROM questions WHERE id=?", qid)["status"] == "open"


def test_no_agent_tool_path_can_resume_or_approve(project):
    """REQ-SAFE-010/020: agents get no tool that resumes, stops everything, or decides a safety
    card — TeamAPI exposes none of those, and resolve_question explicitly refuses safety-kind."""
    cfg, store = project
    api_methods = {name for name in dir(TeamAPI) if not name.startswith("_")}
    assert not api_methods & {"resume", "stop_now", "answer_question", "dismiss_question"}

    qid = store.ask("lead", "Approve?", options=["Approve", "Reject"], kind="safety")
    result = TeamAPI(cfg, store, "lead").resolve_question(qid, "Approve")
    assert result.startswith("ERROR:") and "cannot resolve" in result
    assert store.one("SELECT * FROM questions WHERE id=?", qid)["status"] == "open"
