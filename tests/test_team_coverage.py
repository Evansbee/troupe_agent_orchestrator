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


def test_gui_data_pin_edit_delete_memory(project):
    cfg, store = project
    mid = store.remember("lead", "Decision", content="body", rationale="why")
    d = Data(cfg)
    d.set_memory(mid, pinned=True)
    assert store.memory(mid)["pinned"] == 1
    d.set_memory(mid, title="Edited title")
    assert store.memory(mid)["title"] == "Edited title"
    d.delete_memory(mid)
    assert store.memory(mid) is None


def test_supersede_hides_from_recall_by_default(project):
    cfg, store = project
    lead = TeamAPI(cfg, store, "lead")
    lead.remember("Old decision", rationale="v1")
    old_id = store.memories(limit=1)[0]["id"]
    result = lead.remember("New decision", rationale="v2", supersedes=old_id)
    assert not result.startswith("ERROR")
    new_id = store.memories(limit=1)[0]["id"]
    assert "Old decision" not in lead.recall("Old decision")
    shown = lead.recall("Old decision", include_superseded=True)
    assert "Old decision" in shown
    assert f"[superseded by #{new_id}]" in shown
    err = lead.remember("Another", supersedes=old_id)
    assert err == f"ERROR: memory #{old_id} is already superseded by #{new_id}."
    assert lead.remember("x", supersedes=999999).startswith("ERROR: memory #999999 not found")


def test_agent_cannot_supersede_the_humans_memories(project):
    """REQ-COM-032 Principle 0 guard (QA's #8 finding): superseding is a de-facto delete, so an agent
    can't do it to anything pinned, human-authored, or a preference — only the human can."""
    from troupe.engine import Engine, Wake

    cfg, store = project
    lead = TeamAPI(cfg, store, "lead")
    a = cfg.agent("spec")

    pinned_id = store.remember("lead", "Never push without asking", kind="decision")
    store.update_memory(pinned_id, pinned=True)
    err = lead.remember("Pushing is fine", supersedes=pinned_id)
    assert err == "ERROR: that's the human's — ask the human (ask_human) instead"
    assert store.memory(pinned_id)["superseded_by"] is None
    prompt = Engine(cfg).build_prompt(a, Wake(1, a, "messages"), [], None, {})
    assert "Never push without asking" in prompt  # still pinned in every prompt

    human_authored_id = store.remember("human", "Human's own note", kind="decision")
    err = lead.remember("Overriding it", supersedes=human_authored_id)
    assert err == "ERROR: that's the human's — ask the human (ask_human) instead"

    preference_id = store.remember("lead", "Prefers terse commit messages", kind="preference")
    err = lead.remember("Verbose is fine now", supersedes=preference_id)
    assert err == "ERROR: that's the human's — ask the human (ask_human) instead"

    # Agent-to-agent supersedes of an ordinary decision are unaffected.
    plain_id = store.remember("lead", "Ordinary decision")
    assert not lead.remember("Revised decision", supersedes=plain_id).startswith("ERROR")

    # The human's own supersede still works, through any of the three trigger conditions.
    human = TeamAPI(cfg, store, "human")
    result = human.remember("Pushing needs a heads-up now", supersedes=pinned_id)
    assert not result.startswith("ERROR")
    assert store.memory(pinned_id)["superseded_by"] is not None
