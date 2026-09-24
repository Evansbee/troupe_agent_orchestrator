import json
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from troupe.api import STAGED, APIError, APIServer, socket_path
from troupe.api_client import Client
from troupe.config import AgentCfg, Config
from troupe.store import Store


@pytest.fixture
def api(tmp_path):
    (tmp_path / ".troupe").mkdir()
    cfg = Config(
        tmp_path, "Test Project", [AgentCfg("builder-1", "builder", "Builder", "local")]
    )
    s = Store(cfg.db_path)
    s.sync_agents(cfg.agents)
    server = APIServer(cfg).start()
    try:
        yield server, s
    finally:
        server.stop()


def client(api, **kwargs):
    return Client(api[0].cfg.root, **kwargs)


def error(c, method, params, code):
    with pytest.raises(APIError) as exc:
        c.call(method, params)
    assert exc.value.code == code


def test_handshake_and_lifecycle(api):
    server, s = api
    assert stat.S_IMODE(server.path.stat().st_mode) == 0o600
    with client(api, hello=False) as c:
        assert c.call("ping")["pong"]
        error(c, "snapshot", {}, "handshake_required")
        hello = c.call("hello", dict(api_version=0, client="tests", notifications=True))
        assert hello["handle_suffix"] == "@Test_Project"
        assert server.notifications_suppressed
        error(c, "bogus", {}, "unknown_method")
        error(c, "snapshot", dict(typo=True), "bad_request")
    with client(api, hello=False) as c:
        error(c, "hello", dict(api_version=1, client="tests"), "unsupported_version")
    server.stop()
    assert not server.path.exists()
    assert (
        json.loads((server.cfg.state_dir / "service.json").read_text())["state"]
        == "stopped"
    )


def test_every_read_and_shapes(api):
    server, s = api
    tid = s.add_task("Task", assignee="builder-1")
    s.task_note(tid, "human", "note")
    mid = s.send("builder-1", "human", "hi", kind="chat", task_id=tid)
    qid = s.ask("builder-1", "Which?", options=["A", "B"])
    mem = s.remember("builder-1", "Decision")
    rid = s.start_run("builder-1", "task", tid, str(server.cfg.root), False)
    s.run_line(rid, "text", "hello")
    s.end_run(rid, "failed", 1, 20, "failed")
    with client(api) as c:
        snap = c.call("snapshot")
        assert set(snap) == {
            "seq",
            "server_time",
            "engine",
            "usage",
            "seen",
            "agents",
            "tasks",
            "milestones",
            "questions",
            "recent_messages",
        }
        assert snap["agents"][0]["handle"] == "builder_1@Test_Project"
        assert (
            snap["agents"][0]["mail_reading"] == 0
            and snap["agents"][0]["waiting_on"] is None
        )
        for method in (
            "agents",
            "tasks",
            "milestones",
            "messages",
            "questions",
            "memories",
            "runs",
            "activity",
        ):
            assert isinstance(c.call(method)["items"], list)
        for method in ("engine", "usage", "seen", "config"):
            assert isinstance(c.call(method), dict)
        assert c.call("task", {"id": tid})["notes"][0]["author"] == "human"
        assert c.call("decision_comments", {"decision_id": mem}) == {"items": []}
        lines = c.call("run_lines", {"run_id": rid})
        assert lines["done"] and lines["items"][0]["text"] == "hello"
        assert c.call("runs")["items"][0]["status"] == "error"
        assert (
            c.call(
                "messages",
                {
                    "agent": "builder_1@Test_Project",
                    "chat_with": "builder-1",
                    "kind": "chat",
                    "task_id": tid,
                },
            )["items"][0]["id"]
            == mid
        )
        assert c.call("messages", {"room": "team"})["items"] == []
        assert c.call("questions")["items"][0]["id"] == qid
        for method, p in [
            ("task", {"id": 999}),
            ("run_lines", {"run_id": 999}),
            ("decision_comments", {"decision_id": 999}),
        ]:
            error(c, method, p, "not_found")
        for method, p in [
            ("messages", {"kind": "bad"}),
            ("tasks", {"status": ["bad"]}),
            ("questions", {"status": "bad"}),
            ("runs", {"limit": 201}),
            ("memories", {"major": "yes"}),
        ]:
            error(c, method, p, "bad_request")


def test_update_and_delete_memory(api):
    server, s = api
    mid = s.remember("builder-1", "Decision", content="body", rationale="why")
    with client(api) as c:
        result = c.call("update_memory", dict(id=mid, pinned=True))
        assert result["memory"]["pinned"] is True
        result = c.call("update_memory", dict(id=mid, title="Edited", content="new body"))
        assert result["memory"]["title"] == "Edited"
        assert result["memory"]["content"] == "new body"
        assert result["memory"]["rationale"] == "why"
        error(c, "update_memory", dict(id=mid, major=True), "unavailable")
        error(c, "update_memory", dict(id=999, title="x"), "not_found")
        error(c, "update_memory", dict(id=mid), "bad_request")
        assert c.call("delete_memory", dict(id=mid)) == {"deleted": True}
        error(c, "delete_memory", dict(id=mid), "not_found")


def test_memories_status_filter_excludes_superseded_by_default(api):
    server, s = api
    old = s.remember("builder-1", "Old")
    new = s.remember("builder-1", "New", supersedes=old)
    with client(api) as c:
        active = c.call("memories")["items"]
        assert [m["id"] for m in active] == [new]
        superseded = c.call("memories", dict(status="superseded"))["items"]
        assert [m["id"] for m in superseded] == [old]
        assert superseded[0]["superseded_by"] == new
        assert superseded[0]["status"] == "superseded"
        assert c.call("memories", dict(status="reverted"))["items"] == []


def test_commands_and_idempotency(api):
    server, s = api
    with client(api) as c:
        first = c.call("chat", dict(agent="builder-1", text="hi", idempotency_key="x"))
        assert c.call(
            "chat", dict(agent="builder-1", text="hi", idempotency_key="x")
        ) == dict(first, duplicate=True)
        assert s.scalar("SELECT count(*) FROM messages") == 1
        s.send("builder-1", "human", "reply", kind="chat")
        assert c.call("mark_chat_read", dict(agent="builder-1"))["count"] == 1
        for method, cmd in [
            ("wake", "poke"),
            ("stop_run", "stop"),
            ("new_session", "reset_session"),
        ]:
            assert c.call(method, dict(agent="builder-1"))["accepted"]
            assert s.one("SELECT * FROM commands ORDER BY id DESC")["cmd"] == cmd
        c.call("set_agent_enabled", dict(agent="builder-1", enabled=False))
        assert s.one("SELECT * FROM commands ORDER BY id DESC")["cmd"] == "disable"
        assert c.call("pause")["engine"]["paused"]
        assert not c.call("resume")["engine"]["paused"]
        q = s.ask("builder-1", "A?")
        assert (
            c.call("answer_question", dict(id=q, text="yes"))["question"]["answer"]
            == "yes"
        )
        error(c, "answer_question", dict(id=q, text="again"), "conflict")
        q = s.ask("builder-1", "B?")
        assert (
            c.call("dismiss_question", dict(id=q))["question"]["status"] == "dismissed"
        )
        q = s.ask("builder-1", "Approve?", kind="approval")
        error(c, "dismiss_question", dict(id=q), "forbidden")
        error(c, "answer_question", dict(id=q, text="yes"), "bad_request")
        t = c.call("create_task", dict(title="Build", idempotency_key="task"))["task"]
        assert t["status"] == "ready" and t["created_by"] == "human"
        assert c.call("create_task", dict(title="Build", idempotency_key="task"))[
            "duplicate"
        ]
        c.call(
            "update_task",
            dict(
                id=t["id"],
                fields=dict(assignee="builder_1@Test_Project", status="blocked"),
            ),
        )
        note = c.call(
            "add_task_note", dict(id=t["id"], text="please", idempotency_key="note")
        )["note"]
        assert note["author"] == "human"
        assert c.call(
            "add_task_note", dict(id=t["id"], text="please", idempotency_key="note")
        )["duplicate"]
        assert (
            s.one("SELECT * FROM messages ORDER BY id DESC")["subject"]
            == f"Note on #{t['id']}"
        )
        s.update_task(t["id"], branch="task", attempts=2, next_attempt_at=20)
        assert (
            c.call("update_task", dict(id=t["id"], fields=dict(status="done")))["task"][
                "status"
            ]
            == "approved"
        )
        assert s.task_notes(t["id"])[-1]["text"] == "Approved by the human."
        assert (
            c.call("update_task", dict(id=t["id"], fields=dict(status="ready")))[
                "task"
            ]["attempts"]
            == 0
        )
        error(c, "update_task", dict(id=t["id"], fields=dict(cost=2)), "bad_request")
        error(
            c,
            "update_task",
            dict(id=t["id"], fields=dict(assignee="unknown")),
            "not_found",
        )
        assert (
            c.call("mark_seen", dict(key="human_last_seen", ts=20))["seen"][
                "human_last_seen"
            ]
            == 20
        )
        assert (
            c.call("mark_seen", dict(key="human_last_seen", ts=10))["seen"][
                "human_last_seen"
            ]
            == 20
        )
        for method, (req, task) in STAGED.items():
            with pytest.raises(APIError) as e:
                c.call(method)
            assert e.value.code == "unavailable" and e.value.data == dict(
                req=req, task=task
            )
    with client(api) as c:
        assert c.call("chat", dict(agent="builder-1", text="hi", idempotency_key="x"))[
            "duplicate"
        ]
    s.x("UPDATE api_idempotency SET ts=0")
    with client(api) as c:
        assert "duplicate" not in c.call(
            "chat", dict(agent="builder-1", text="hi", idempotency_key="x")
        )


def test_subscription_replay_and_external_writer(api):
    server, s = api
    with client(api) as c:
        seq = c.call("snapshot")["seq"]
        c.call("subscribe", dict(topics=["message.new"], since_seq=seq))
        code = """import sys,time
from troupe.store import Store
s=Store(sys.argv[1])
for i in range(50):
 s.send('human','builder-1',str(time.time()))
 time.sleep(.01)
"""
        process = subprocess.Popen([sys.executable, "-c", code, s.path])
        latency = []
        for _ in range(50):
            e = c.event()
            assert e["event"] == "message.new"
            latency.append(time.time() - float(e["data"]["message"]["body"]))
        assert process.wait(timeout=5) == 0
        assert sorted(latency)[47] < 0.25
        seq = e["seq"]
    for i in range(5):
        s.send("human", "builder-1", f"replay{i}")
    with client(api) as c:
        c.call("subscribe", dict(topics=["message.new"], since_seq=seq))
        assert [c.event()["data"]["message"]["body"] for _ in range(5)] == [
            f"replay{i}" for i in range(5)
        ]
        c.call("unsubscribe")
        error(c, "subscribe", dict(since_seq=server.seq + 1), "resync_required")
        server.loop.call_soon_threadsafe(server.ring.clear)
        time.sleep(0.02)
        error(c, "subscribe", dict(since_seq=0), "resync_required")


def test_stale_socket_and_long_path(tmp_path, monkeypatch):
    home = Path(tempfile.mkdtemp(prefix="api-", dir="/tmp"))
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / ("x" * 100)
    (root / ".troupe").mkdir(parents=True)
    cfg = Config(root, "Test", [])
    Store(cfg.db_path)
    path = socket_path(root, create=True)
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))
    stale.close()
    server = APIServer(cfg).start()
    try:
        assert server.path == path and socket_path(root) == path
        with Client(root) as c:
            assert c.call("ping")["pong"]
        with pytest.raises(RuntimeError, match="another API"):
            APIServer(cfg).start()
        with Client(root) as c:
            assert c.call("ping")["pong"]
    finally:
        server.stop()


def test_malformed_oversize_and_truncation(api):
    server, s = api
    for raw in (b"not json\n", b"x" * (4 * 1024 * 1024 + 1) + b"\n"):
        sock = socket.socket(socket.AF_UNIX)
        sock.connect(str(server.path))
        f = sock.makefile("rwb")
        try:
            try:
                f.write(raw)
                f.flush()
            except BrokenPipeError:
                pass
            response = json.loads(f.readline())
            assert response["id"] is None and response["error"]["code"] == "bad_request"
            assert not f.readline()
        finally:
            f.close()
            sock.close()
    s.send("human", "builder-1", "é" * (1024 * 1024))
    with client(api) as c:
        m = c.call("messages")["items"][0]
        assert m["truncated"] and len(m["body"].encode()) <= 1024 * 1024


def test_snapshot_performance_and_paging(api):
    server, s = api
    s.conn.execute("BEGIN")
    for i in range(500):
        s.add_task(str(i))
    for i in range(5000):
        s.x(
            "INSERT INTO messages(ts,sender,recipient,body) VALUES(1,'human','builder-1',?)",
            str(i),
        )
    s.conn.commit()
    # Drain accumulated events before measuring steady-state snapshot work.
    with client(api) as c:
        c.call("snapshot")
        start = time.perf_counter()
        snap = c.call("snapshot")
        assert time.perf_counter() - start < 0.2
        assert len(snap["tasks"]) == 500
        start = time.perf_counter()
        page = c.call("messages", dict(limit=500))
        assert time.perf_counter() - start < 0.1
        assert len(page["items"]) == 500
        second = c.call("messages", dict(limit=500, before_id=page["next_before_id"]))
        assert second["items"][0]["id"] < page["items"][-1]["id"]


def test_slow_consumer_resync_and_engine_independence(api):
    server, s = api
    server.push_limit = 4096
    with client(api) as c:
        c.call("subscribe", dict(topics=["message.new"]))
        s.conn.execute("BEGIN")
        for _ in range(20):
            s.send("human", "builder-1", "x" * 1024)
        s.conn.commit()
        time.sleep(0.2)
        # The queue is discarded before the writer gets a chance to drain it.
        assert c.event()["event"] == "resync_required"
        assert c.call("ping")["pong"]
        assert not next(iter(server.connections)).subscribed


def test_change_events_and_snapshot_boundary(api):
    server, s = api
    with client(api) as c:
        seq = c.call("snapshot")["seq"]
        tid = s.add_task("before subscribe")
        c.call(
            "subscribe",
            dict(
                since_seq=seq,
                topics=[
                    "task.changed",
                    "question.*",
                    "memory.*",
                    "run.*",
                    "seen.changed",
                ],
            ),
        )
        assert c.event()["data"]["task"]["id"] == tid
        qid = s.ask("builder-1", "Answer?")
        assert c.event()["event"] == "question.new"
        s.answer(qid, "yes")
        assert c.event()["event"] == "question.answered"
        mid = s.remember("builder-1", "a")
        assert c.event()["event"] == "memory.new"
        s.x("DELETE FROM memories WHERE id=?", mid)
        assert c.event()["data"]["memory"] == dict(id=mid, deleted=True)
        rid = s.start_run("builder-1", "task", tid, str(server.cfg.root), False)
        assert c.event()["event"] == "run.started"
        s.run_line(rid, "text", "line")
        assert c.event()["data"]["line"]["text"] == "line"
        s.end_run(rid, "ok", 0, 0, "done")
        assert c.event()["event"] == "run.finished"
        c.call("mark_seen", dict(key="decisions_seen_at", ts=100))
        assert c.event()["data"]["seen"]["decisions_seen_at"] == 100
        s.task_note(tid, "qa", "REVIEW APPROVE: verified")
        assert c.event()["data"]["task"]["notes_count"] == 1
        assert c.call("task", dict(id=tid))["reviews"][0]["verdict"] == "approve"


def test_config_redaction_usage_and_validation(api):
    server, s = api
    server.cfg.toml_data = {
        "backends": {"local_api_key": "secret", "nested": {"password": "private"}},
        "environment": {"HOME": "private"},
        "git": {"check": "pytest"},
    }
    s.kv_set(
        "claude_ratelimit",
        dict(
            unifiedWindows=dict(
                five_hour=dict(utilization=0.25, resetsAt="2026-09-24T12:00:00Z")
            )
        ),
    )
    with client(api) as c:
        config = c.call("config")["troupe_toml"]
        assert config["backends"]["local_api_key"] == "***"
        assert config["backends"]["nested"]["password"] == "***"
        assert config["environment"] == "***" and config["git"]["check"] == "pytest"
        window = c.call("usage")["providers"][0]["windows"][0]
        assert window["used_pct"] == 25 and window["resets_at"] > 0
        for method, params in [
            ("create_task", {"title": 5}),
            ("create_task", {"title": "x", "role": "bad"}),
            ("chat", {"agent": "builder-1", "text": "x", "idempotency_key": "x" * 65}),
            ("mark_seen", {"key": "other"}),
            ("subscribe", {"topics": [1]}),
            ("set_agent_enabled", {"agent": "builder-1", "enabled": 1}),
        ]:
            error(c, method, params, "bad_request")
        assert not s.tasks()


def test_cli_and_engine_lifecycle(tmp_path, monkeypatch, capsys):
    from troupe.api_client import cli
    from troupe.engine import Engine

    (tmp_path / ".troupe").mkdir()
    cfg = Config(tmp_path, "engine", [])
    engine = Engine(cfg)
    monkeypatch.setattr(engine, "recover", lambda: None)
    beats = []

    async def tick():
        beats.append(time.time())
        engine.store.kv_set("heartbeat", time.time())

    monkeypatch.setattr(engine, "tick", tick)
    monkeypatch.setattr("troupe.engine.TICK", 0.05)
    thread = engine.start_thread()
    deadline = time.time() + 1
    while not getattr(engine, "api", None) or not engine.api._ready.is_set():
        assert time.time() < deadline
        time.sleep(0.005)
    try:
        assert cli(tmp_path, "ping") == 0
        assert "pong" in capsys.readouterr().out
        assert cli(tmp_path, "unknown") == 1
        assert "unknown_method" in capsys.readouterr().err
        with Client(tmp_path) as c:
            c.call("subscribe")
            # A non-reading client cannot delay scheduler ticks.
            for i in range(50):
                engine.store.send("human", "someone", "x" * 10000)
            time.sleep(0.3)
        assert len(beats) >= 4 and max(b - a for a, b in zip(beats, beats[1:])) < 0.25
    finally:
        engine.stop()
        thread.join(timeout=3)
    assert not thread.is_alive() and not socket_path(tmp_path).exists()
    assert cli(tmp_path, "ping") == 2
    assert "engine not running" in capsys.readouterr().err


def test_default_backpressure_threshold(api):
    server, s = api
    with client(api) as c:
        c.call("subscribe", {"topics": ["message.new"]})
        s.conn.execute("BEGIN")
        for _ in range(12):
            s.x(
                "INSERT INTO messages(ts,sender,recipient,body) VALUES(1,'human','builder-1',?)",
                "x" * (1024 * 1024),
            )
        s.conn.commit()
        time.sleep(0.3)
        event = c.event()
        assert event["event"] == "resync_required"
        assert c.call("ping")["pong"]


def test_internal_error_logs_traceback(api, monkeypatch):
    server, _ = api
    original = server.data.read

    def broken(method, params):
        if method == "config":
            raise RuntimeError("test failure")
        return original(method, params)

    monkeypatch.setattr(server.data, "read", broken)
    with client(api) as c:
        error(c, "config", {}, "internal")
        assert c.call("ping")["pong"]
    log = (server.cfg.state_dir / "engine.log").read_text()
    assert "Traceback" in log and "test failure" in log


def test_run_provider_is_not_rewritten_by_roster_changes(api):
    _, s = api
    s.set_agent("builder-1", model="original")
    rid = s.start_run("builder-1", "task", None, "/tmp", False)
    s.set_agent("builder-1", backend="claude", model="new")
    with client(api) as c:
        run = c.call("runs")["items"][0]
        assert run["id"] == rid
        assert (run["provider"], run["model"]) == ("local", "original")
