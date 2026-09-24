"""Verification of shipped communication and tool permission requirements."""
import pytest

from troupe.gui.data import Data
from troupe.team import TeamAPI


def test_mcp_environment_identity(project, monkeypatch):
    from troupe import mcp_server
    cfg, _ = project
    seen = []
    class Server:
        def run(self, transport):
            seen.append(transport)
    def build(root, agent):
        seen.extend([root, agent])
        return Server(), None
    monkeypatch.setenv("TROUPE_ROOT", str(cfg.root))
    monkeypatch.setenv("TROUPE_AGENT", "qa")
    monkeypatch.setattr(mcp_server, "build_server", build)
    mcp_server.main()
    assert seen == [cfg.root, "qa", "stdio"]


def test_errors_offer_recipient_and_status_guidance(project):
    cfg, store = project
    api = TeamAPI(cfg, store, "lead")
    error = api.send_message("missing", "hello")
    assert error.startswith("ERROR:") and "Use an agent id" in error
    tid = store.add_task("Task")
    error = api.update_task(tid, status="invalid")
    assert error.startswith("ERROR:") and "use one of" in error
    assert store.task(tid)["status"] == "backlog"


def test_permission_boundaries(project):
    cfg, store = project
    tid = store.add_task("Task", assignee="builder-1", status="in_progress")
    owner = TeamAPI(cfg, store, "builder-1")
    other = TeamAPI(cfg, store, "builder-2")
    for change in ({"priority": 0}, {"assignee": "builder-2"}, {"status": "done"}):
        assert owner.update_task(tid, **change).startswith("ERROR:")
    assert other.update_task(tid, status="blocked").startswith("ERROR:")
    for status in ("blocked", "in_progress"):
        assert not owner.update_task(tid, status=status).startswith("ERROR:")
        assert store.task(tid)["status"] == status
    store.update_task(tid, status="review")
    assert other.review_task(tid, "approve", "pass").startswith("ERROR:")
    assert store.task(tid)["status"] == "review"
    assert not TeamAPI(cfg, store, "lead").review_task(tid, "approve", "pass").startswith("ERROR:")
    assert not TeamAPI(cfg, store, "human").update_task(tid, priority=0).startswith("ERROR:")


@pytest.mark.parametrize("recipient", ["pm", "human", "builder", "team"])
def test_mail_routing_events_and_inbox_consumption(project, recipient):
    cfg, store = project
    api = TeamAPI(cfg, store, "lead")
    result = api.send_message(recipient, "Body", subject="Subject")
    assert isinstance(result, str) and not result.startswith("ERROR:")
    expected = ({"pm"} if recipient == "pm" else {"human"} if recipient == "human" else
                {"builder-1", "builder-2"} if recipient == "builder" else
                {a.id for a in cfg.agents if a.id != "lead"})
    assert {m["recipient"] for m in store.messages()} == expected
    assert len([e for e in store.events() if e["kind"] == "message"]) == len(expected)
    for target in expected:
        assert "Body" in TeamAPI(cfg, store, target).check_inbox()
        assert store.unread(target) == []


def test_idea_options_and_dismissal_notification(project):
    cfg, store = project
    TeamAPI(cfg, store, "pm").propose_idea("Idea", "Pitch", "Reason")
    q = store.questions()[0]
    assert q["kind"] == "idea"
    assert q["options"] == ["Yes, do it", "No", "Later", "Sort of — let's discuss"]
    Data(cfg).dismiss(q["id"])
    assert store.questions() == []
    assert "use your judgment" in store.unread("pm")[-1]["body"]


def test_memory_visibility_rationale_and_answer_search(project):
    cfg, store = project
    spec = TeamAPI(cfg, store, "spec")
    lead = TeamAPI(cfg, store, "lead")
    spec.remember("Shared decision", rationale="Reason")
    spec.remember("Secret note", private=True, kind="note")
    assert "Reason" in lead.recall("Shared")
    assert "Secret note" not in lead.recall("Secret")
    assert "Secret note" in spec.recall("Secret")
    qid = store.ask("spec", "Storage choice?")
    store.answer(qid, "SQLite")
    assert "answered question" in lead.recall("storage sqlite")
    assert "SQLite" in lead.recall("storage sqlite")
