"""#57: the API's stop_now/resume/safety-approval commands wired to #42's shared safety paths
(safety.stop_now/resume, gates.py's answer-text verdict parsing), not a second implementation —
and (QA's Principle 0 finding on the first cut) human-only commands refuse an agent caller.
REQ-SAFE-010 (kill switch), REQ-SAFE-020 (approval cards), REQ-SAFE-040 (audit).
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from troupe.api import APIError, APIServer
from troupe.api_client import Client
from troupe.engine import Engine, Wake
from troupe.gates import verdict
from troupe.runners import Runner, RunResult
from troupe.team import TeamAPI

CALLER_SCRIPT = Path(__file__).parent / "_api_call_subprocess.py"


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


def call_over_socket(root, method, params=None, *, agent: str | None = None) -> dict:
    """Makes one call from a genuinely separate process — required to test the peer-pid check at
    all: on macOS `ps` (which that check reads) reports a process's environment as of exec() time,
    so monkeypatching os.environ in the already-running test process can't simulate "no
    TROUPE_AGENT" for it. `agent=None` means the subprocess's env has no TROUPE_AGENT (a human
    caller); `agent="some-id"` sets it (an agent's shell command)."""
    env = dict(os.environ)
    if agent is None:
        env.pop("TROUPE_AGENT", None)
    else:
        env["TROUPE_AGENT"] = agent
    proc = subprocess.run(
        [sys.executable, str(CALLER_SCRIPT), str(root), method, json.dumps(params or {})],
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def assert_forbidden(outcome):
    assert outcome == dict(outcome, ok=False, code="forbidden"), outcome


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
        # #71: two real subprocess spawns race OS scheduling under load — poll generously (4s)
        # rather than the original tight 1s budget. `start` is captured after setup finishes, so
        # widening this doesn't eat into the "within 2s" budget the test name promises below.
        for _ in range(400):
            if all(r.proc for r, _, _ in running):
                break
            await asyncio.sleep(.01)
        await asyncio.sleep(.5)  # children install their SIGTERM-ignoring handler (no observable to poll)

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


def test_stop_now_from_an_agent_caller_still_works(api):
    """stop_now stays open to agents (lead's decision on QA's finding) — stopping is the safe
    direction, unlike resume or approving a protected merge."""
    server, s = api
    root = server.cfg.root
    result = call_over_socket(root, "stop_now", agent="designer")
    assert result["ok"] is True and result["result"] == {"killed": 0}
    assert s.kv_get("stopped") and s.kv_get("paused")


def test_resume_over_the_api_clears_stopped_and_is_audited(api):
    server, s = api
    root = server.cfg.root
    with client(api) as c:
        c.call("stop_now")
        assert s.kv_get("stopped") and s.kv_get("paused")
    result = call_over_socket(root, "resume")  # agent=None: a human-driven call
    assert result["ok"] is True
    assert result["result"]["engine"]["stopped"] is False
    assert result["result"]["engine"]["paused"] is False
    assert not s.kv_get("stopped") and not s.kv_get("paused")
    assert any("resumed" in e["text"].lower() and e["kind"] == "safety" for e in s.events(limit=20))


def test_resume_is_forbidden_from_an_agent_caller(api):
    """QA's Principle 0 repro: an agent's shell command must not be able to undo the kill switch."""
    server, s = api
    root = server.cfg.root
    with client(api) as c:
        c.call("stop_now")
    assert s.kv_get("stopped") and s.kv_get("paused")
    result = call_over_socket(root, "resume", agent="designer")
    assert_forbidden(result)
    assert s.kv_get("stopped") and s.kv_get("paused")  # unchanged — the kill switch still holds


def test_plain_pause_does_not_touch_the_kill_switch(api):
    server, s = api
    root = server.cfg.root
    with client(api) as c:
        assert c.call("pause")["engine"]["paused"] is True
        assert s.kv_get("stopped") in (None, False)
    result = call_over_socket(root, "resume")
    assert result["ok"] is True and result["result"]["engine"]["paused"] is False


def test_answer_question_approves_a_safety_card_through_gates_verdict(api):
    server, s = api
    root = server.cfg.root
    qid = s.ask("system", "Approve protected changes?", options=["Approve", "Reject"], kind="safety")
    result = call_over_socket(root, "answer_question", dict(id=qid, decision="approve", text=""))
    assert result["ok"] is True
    assert result["result"]["question"]["status"] == "answered"
    q = s.one("SELECT * FROM questions WHERE id=?", qid)
    assert q["answer"] == "Approve"
    assert verdict(s, dict(qid=qid)) == "approve"
    assert any(e["kind"] == "safety" and "approve" in e["text"].lower() for e in s.events(limit=20))


def test_approve_safety_card_is_forbidden_from_an_agent_caller(api):
    """QA's Principle 0 repro: an agent's shell command must not be able to approve a protected
    merge as the human."""
    server, s = api
    root = server.cfg.root
    qid = s.ask("system", "Approve protected changes?", options=["Approve", "Reject"], kind="safety")
    result = call_over_socket(root, "answer_question", dict(id=qid, decision="approve", text=""), agent="designer")
    assert_forbidden(result)
    q = s.one("SELECT * FROM questions WHERE id=?", qid)
    assert q["status"] == "open"
    assert verdict(s, dict(qid=qid)) == ""  # neither approved nor rejected


def test_answer_question_rejects_a_safety_card_with_a_note(api):
    server, s = api
    root = server.cfg.root
    qid = s.ask("system", "Approve protected changes?", options=["Approve", "Reject"], kind="safety")
    result = call_over_socket(root, "answer_question", dict(id=qid, decision="reject", text="Too risky"))
    assert result["ok"] is True
    assert result["result"]["question"]["answer"] == "Reject — Too risky"
    assert verdict(s, dict(qid=qid)) == "reject"


def test_approval_kind_question_also_wired_the_same_way(api):
    """`kind="approval"` (the API's own display label) hits the same code path as the stored `safety`
    kind — both are gated the same way in command()."""
    server, s = api
    root = server.cfg.root
    qid = s.ask("system", "Approve?", options=["Approve", "Reject"], kind="approval")
    result = call_over_socket(root, "answer_question", dict(id=qid, decision="approve", text=""))
    assert result["ok"] is True
    assert s.one("SELECT * FROM questions WHERE id=?", qid)["answer"] == "Approve"


def test_safety_card_cannot_be_dismissed_or_answered_without_a_decision(api):
    server, s = api
    root = server.cfg.root
    qid = s.ask("system", "Approve?", options=["Approve", "Reject"], kind="safety")
    dismissed = call_over_socket(root, "dismiss_question", dict(id=qid))
    assert dismissed == dict(dismissed, ok=False, code="forbidden")
    no_decision = call_over_socket(root, "answer_question", dict(id=qid, text="sure"))
    assert no_decision == dict(no_decision, ok=False, code="bad_request")
    bad_decision = call_over_socket(root, "answer_question", dict(id=qid, decision="maybe", text=""))
    assert bad_decision == dict(bad_decision, ok=False, code="bad_request")
    assert s.one("SELECT * FROM questions WHERE id=?", qid)["status"] == "open"


def test_dismiss_question_is_forbidden_from_an_agent_caller_for_any_kind(api):
    """QA: "agents must not answer the human's questions as the human either" — not just safety
    cards. An ordinary question, asked by one agent for the human, must not be dismissable by any
    agent's shell command."""
    server, s = api
    root = server.cfg.root
    qid = s.ask("lead", "Ship the v2 API today?")
    result = call_over_socket(root, "dismiss_question", dict(id=qid), agent="designer")
    assert_forbidden(result)
    assert s.one("SELECT * FROM questions WHERE id=?", qid)["status"] == "open"


def test_answer_question_is_forbidden_from_an_agent_caller_for_a_plain_question(api):
    server, s = api
    root = server.cfg.root
    qid = s.ask("lead", "Ship the v2 API today?")
    result = call_over_socket(root, "answer_question", dict(id=qid, text="Yes"), agent="designer")
    assert_forbidden(result)
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
