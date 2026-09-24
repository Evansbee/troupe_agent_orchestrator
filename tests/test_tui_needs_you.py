"""REQ-TUI-010 (Needs-you)/020 (`a` answering). Pilot tests against a fixture API — a fake client that
replays api.py's own rules (dismiss forbidden for approval-kind, decision required for it) — since
#66's real TuiClient/app.py don't exist yet (built in parallel; see tui/panes/needs_you.py's docstring).

No async test functions: the rest of this suite drives asyncio with `asyncio.run()` inside plain
`def test_...()`, not a pytest-asyncio/anyio marker, so this follows the same convention.
"""
from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Input, ListView

from troupe.tui.panes.needs_you import NeedsYouPane, QuestionCard


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class FakeClient:
    """Mimics just enough of the real API's `questions`/`answer_question`/`dismiss_question` (REQ-API-040/042)
    to drive the pane: approval-kind questions require `decision`, and can't be dismissed."""

    def __init__(self, questions: list[dict]):
        self.questions = {q["id"]: dict(q) for q in questions}
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, timeout: float = 5.0, **params) -> dict:
        self.calls.append((method, dict(params)))
        if method == "questions":
            return {"items": [q for q in self.questions.values() if q["status"] == "open"]}
        if method == "answer_question":
            q = self.questions[params["id"]]
            if q["status"] != "open":
                raise ApiError("conflict", "already answered")
            if q["kind"] in ("approval", "safety"):
                if params.get("decision") not in ("approve", "reject"):
                    raise ApiError("bad_request", "decision must be approve or reject")
            elif "decision" in params:
                raise ApiError("bad_request", "decision only applies to approval questions")
            q["status"] = "answered"
            q["answer"] = params.get("text") or params.get("decision", "")
            return {"question": q}
        if method == "dismiss_question":
            q = self.questions[params["id"]]
            if q["kind"] in ("approval", "safety"):
                raise ApiError("forbidden", "approval questions cannot be dismissed")
            q["status"] = "dismissed"
            return {"question": q}
        raise ApiError("unknown_method", method)

    def answer_elsewhere(self, question_id: int) -> dict:
        """Simulate another client answering a card — the way a live `question.answered` event would."""
        q = self.questions[question_id]
        q["status"] = "answered"
        q["answer"] = "Answered elsewhere"
        return dict(q)


def question(id, kind="question", options=None, context="", **extra):
    return dict(id=id, ts=0, asker="lead", kind=kind, question=f"Q{id}?",
               context=context, options=options or ["Yes", "No"], status="open",
               answer=None, answered_at=None, task_id=None, **extra)


def safety_question(id, paths=("src/troupe/runners.py",)):
    return question(id, kind="safety", options=["Approve", "Reject"],
                    context=f"Protected files:\n{chr(10).join(paths)}\nAdded / removed lines:\n+3 -1",
                    approval=dict(task_id=99, branch="troupe/t99", paths=list(paths)))


class NeedsYouTestApp(App):
    """A standalone harness App — not tui/app.py (#66's territory) — that mounts only this pane."""

    def __init__(self, client: FakeClient):
        super().__init__()
        self.client = client

    def compose(self) -> ComposeResult:
        yield NeedsYouPane(self.client)

    async def on_mount(self) -> None:
        await self.query_one(NeedsYouPane).load()


def run(coro):
    return asyncio.run(coro)


def test_load_renders_open_questions_and_safety_cards():
    async def body():
        client = FakeClient([question(1), safety_question(2)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            cards = pilot.app.query(QuestionCard)
            assert {c.question["id"] for c in cards} == {1, 2}
            safety_card = next(c for c in cards if c.question["id"] == 2)
            assert safety_card.is_safety
            assert "safety-card" in safety_card.classes
            regular_card = next(c for c in cards if c.question["id"] == 1)
            assert not regular_card.is_safety and "safety-card" not in regular_card.classes

    run(body())


def test_answer_by_number_key_after_pressing_a():
    async def body():
        client = FakeClient([question(1, options=["Yes", "No"])])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.query_one("#ny-cards", ListView).focus()
            await pilot.press("a")
            assert pane.answering is True
            await pilot.press("2")
            await pilot.press("enter")
            await pilot.pause()
            assert client.calls[-1] == ("answer_question", {"id": 1, "text": "No"})
            assert pane.answering is False  # answering mode closes after submitting

    run(body())


def test_digit_leading_reply_is_sent_verbatim_not_split_into_two_answers():
    """QA #67 bug A/C: no instant-digit path, so "3 but only after lunch" must go through whole, and
    the embedded "a"/"d" must never re-arm answering or fire a dismiss."""
    async def body():
        client = FakeClient([question(1, options=["one builder", "two builders", "three builders"])])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.query_one("#ny-cards", ListView).focus()
            await pilot.press("a")
            await pilot.press(*"3 but only after lunch")
            await pilot.press("enter")
            await pilot.pause()
            assert client.calls[-1] == ("answer_question", {"id": 1, "text": "3 but only after lunch"})
            answer_calls = [c for c in client.calls if c[0] == "answer_question"]
            assert len(answer_calls) == 1  # not split into "3" and a second reply from "after..."
            assert pane.answering is False

    run(body())


def test_digit_leading_reply_with_trailing_d_never_dismisses():
    """QA #67 bug C: "2 days" must not answer on "2" and then have the "d" fire dismiss_focused."""
    async def body():
        client = FakeClient([question(1, options=["1 day", "2 days"])])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.query_one("#ny-cards", ListView).focus()
            await pilot.press("a")
            await pilot.press(*"2 days")
            await pilot.press("enter")
            await pilot.pause()
            assert client.calls == [("questions", {"status": "open", "limit": 1000}),
                                    ("answer_question", {"id": 1, "text": "2 days"})]
            assert client.questions[1]["status"] == "answered"

    run(body())


def test_digits_typed_before_pressing_a_do_nothing():
    async def body():
        client = FakeClient([question(1, options=["Yes", "No"])])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.query_one("#ny-cards", ListView).focus()
            await pilot.press("2")
            await pilot.pause()
            assert not any(m == "answer_question" for m, _ in client.calls)
            assert pane.answering is False

    run(body())


def test_answer_target_is_captured_at_a_not_at_enter():
    """QA #67 bug D: focus card #2, press `a`; while typing, card #1 is answered elsewhere and its
    removal shifts list indices. The answer must still land on #2, never on whatever is now
    highlighted by index."""
    async def body():
        client = FakeClient([question(1), safety_question(2), safety_question(3)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            list_view = pane.query_one("#ny-cards", ListView)
            list_view.focus()
            for i, item in enumerate(list_view.children):
                if item.question["id"] == 2:
                    list_view.index = i
            await pilot.pause()
            await pilot.press("a")
            assert pane._answering_id == 2
            await pilot.press(*"approve")
            # #1 gets answered by someone else while the human is still typing; its removal shifts
            # index-based lookups toward whatever was after it (e.g. card #3).
            answered = client.answer_elsewhere(1)
            pane.on_troupe_event({"event": "question.answered", "data": {"question": answered}})
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            method, params = client.calls[-1]
            assert method == "answer_question"
            assert params["id"] == 2  # not 3, even though #3 may now sit where #2's index once pointed
            assert client.questions[3]["status"] == "open"

    run(body())


def test_answer_target_gone_sends_nothing():
    """The focused card is answered elsewhere while the human is still typing a reply to it — on
    Enter, the pane must notice the target is gone rather than silently doing nothing useful or,
    worse, resurrecting a stale action against a since-closed card."""
    async def body():
        client = FakeClient([question(1)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.query_one("#ny-cards", ListView).focus()
            await pilot.press("a")
            await pilot.press(*"sure")
            answered = client.answer_elsewhere(1)
            pane.on_troupe_event({"event": "question.answered", "data": {"question": answered}})
            await pilot.pause()
            calls_before = len(client.calls)
            await pilot.press("enter")
            await pilot.pause()
            assert len(client.calls) == calls_before  # no new call — the target no longer exists
            assert "answered elsewhere" in pane.status

    run(body())


def test_digits_do_not_fire_while_reply_box_is_focused():
    async def body():
        client = FakeClient([question(1, options=["Yes", "No"])])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.query_one("#ny-cards", ListView).focus()
            await pilot.press("a")
            answer_input = pane.query_one("#ny-answer", Input)
            assert pilot.app.focused is answer_input
            await pilot.press("3")  # typed into the box — must not answer (there is no option 3 anyway)
            await pilot.pause()
            assert not any(m == "answer_question" for m, _ in client.calls)
            assert answer_input.value == "3"

    run(body())


def test_free_text_reply_submits_custom_answer():
    async def body():
        client = FakeClient([question(1)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.query_one("#ny-cards", ListView).focus()
            await pilot.press("a")
            await pilot.press(*"Sounds good")
            await pilot.press("enter")
            await pilot.pause()
            assert client.calls[-1] == ("answer_question", {"id": 1, "text": "Sounds good"})

    run(body())


def test_dismiss_on_regular_question_calls_dismiss_question():
    async def body():
        client = FakeClient([question(1)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.query_one("#ny-cards", ListView).focus()
            await pilot.press("d")
            await pilot.pause()
            assert client.calls[-1] == ("dismiss_question", {"id": 1})
            assert client.questions[1]["status"] == "dismissed"

    run(body())


def test_dismiss_on_safety_card_asks_for_confirmation_first():
    """QA #67 ask: `d` on a safety card must confirm (y/N) before rejecting, like the kill switch."""
    async def body():
        client = FakeClient([safety_question(2)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.query_one("#ny-cards", ListView).focus()
            await pilot.press("d")
            await pilot.pause()
            assert not any(m == "answer_question" for m, _ in client.calls)
            assert "Reject #2" in pane.status
            assert client.questions[2]["status"] == "open"

    run(body())


def test_confirming_reject_with_y_submits_reject():
    async def body():
        client = FakeClient([safety_question(2)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.query_one("#ny-cards", ListView).focus()
            await pilot.press("d")
            await pilot.press("y")
            await pilot.pause()
            method, params = client.calls[-1]
            assert method == "answer_question"
            assert params["id"] == 2 and params["decision"] == "reject"
            assert client.questions[2]["status"] == "answered"

    run(body())


def test_declining_reject_confirmation_with_n_sends_nothing():
    async def body():
        client = FakeClient([safety_question(2)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.query_one("#ny-cards", ListView).focus()
            await pilot.press("d")
            await pilot.press("n")
            await pilot.pause()
            assert not any(m == "answer_question" for m, _ in client.calls)
            assert client.questions[2]["status"] == "open"
            assert pane._confirm_reject_id is None

    run(body())


def test_escape_declines_reject_confirmation():
    async def body():
        client = FakeClient([safety_question(2)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.query_one("#ny-cards", ListView).focus()
            await pilot.press("d")
            await pilot.press("escape")
            await pilot.pause()
            assert not any(m == "answer_question" for m, _ in client.calls)
            assert pane._confirm_reject_id is None

    run(body())


def test_safety_card_approve_uses_decision_param():
    async def body():
        client = FakeClient([safety_question(2)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.query_one("#ny-cards", ListView).focus()
            await pilot.press("a")
            await pilot.press("1")
            await pilot.press("enter")
            await pilot.pause()
            method, params = client.calls[-1]
            assert method == "answer_question"
            assert params["id"] == 2 and params["decision"] == "approve"

    run(body())


def test_safety_cards_are_never_batch_answered():
    """Two open safety cards; answering the focused one must never touch the other."""
    async def body():
        client = FakeClient([safety_question(2), safety_question(3)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            list_view = pane.query_one("#ny-cards", ListView)
            list_view.focus()
            list_view.index = 0
            await pilot.pause()
            await pilot.press("a")
            await pilot.press("1")
            await pilot.press("enter")
            await pilot.pause()
            answer_calls = [p for m, p in client.calls if m == "answer_question"]
            assert len(answer_calls) == 1
            answered_ids = {q["id"] for q in client.questions.values() if q["status"] == "answered"}
            assert len(answered_ids) == 1
            assert any(q["status"] == "open" for q in client.questions.values())

    run(body())


def test_escape_cancels_answering_without_calling_the_api():
    async def body():
        client = FakeClient([question(1)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.query_one("#ny-cards", ListView).focus()
            await pilot.press("a")
            assert pane.answering is True
            await pilot.press("escape")
            await pilot.pause()
            assert pane.answering is False
            assert not any(m == "answer_question" for m, _ in client.calls)

    run(body())


def test_on_troupe_event_adds_and_removes_cards_live():
    async def body():
        client = FakeClient([question(1)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.on_troupe_event({"event": "question.new", "data": {"question": question(5)}})
            await pilot.pause()
            assert {c.question["id"] for c in pane.query(QuestionCard)} == {1, 5}
            answered = question(1)
            answered["status"] = "answered"
            pane.on_troupe_event({"event": "question.answered", "data": {"question": answered}})
            await pilot.pause()
            assert {c.question["id"] for c in pane.query(QuestionCard)} == {5}

    run(body())


def test_highlight_follows_focused_id_when_a_different_card_is_removed():
    """QA #67 "ideally": focus card #3; #1 (above it) is answered elsewhere. #3 must stay highlighted
    even though its index shifted, not whatever card now sits at #3's old index."""
    async def body():
        client = FakeClient([question(1), question(2), question(3)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            list_view = pane.query_one("#ny-cards", ListView)
            list_view.focus()
            for i, item in enumerate(list_view.children):
                if item.question["id"] == 3:
                    list_view.index = i
            await pilot.pause()
            assert pane._focused_question_id() == 3
            answered = client.answer_elsewhere(1)
            pane.on_troupe_event({"event": "question.answered", "data": {"question": answered}})
            await pilot.pause()
            assert pane._focused_question_id() == 3

    run(body())


def test_unavailable_response_shows_status_and_never_crashes():
    class UnavailableClient(FakeClient):
        async def call(self, method, timeout=5.0, **params):
            if method == "answer_question" and "decision" in params:
                raise ApiError("unavailable", "answer_question is not available yet (task #57)")
            return await super().call(method, timeout=timeout, **params)

    async def body():
        client = UnavailableClient([safety_question(2)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            pane = pilot.app.query_one(NeedsYouPane)
            pane.query_one("#ny-cards", ListView).focus()
            await pilot.press("a")
            await pilot.press("1")
            await pilot.press("enter")
            await pilot.pause()
            assert "Not available yet" in pane.status
            # card is untouched — the pane didn't pretend the approval went through
            assert client.questions[2]["status"] == "open"
            assert {c.question["id"] for c in pane.query(QuestionCard)} == {2}

    run(body())


def test_snapshot_renders_without_error(tmp_path):
    async def body():
        client = FakeClient([question(1), safety_question(2)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            svg = pilot.app.export_screenshot()
            assert "<svg" in svg
            (tmp_path / "needs_you.svg").write_text(svg)

    run(body())


def _baseline_question(previous, proposed):
    return question(3, kind="safety", options=["Approve", "Reject"],
                    context=f"Approved:\n{previous}\nProposed:\n{proposed}",
                    approval=dict(task_id=None, branch=None, paths=[],
                                  previous=previous, proposed=proposed))


def test_changed_baseline_card_shows_which_role_gained_access():
    """#85 round 2 (QA reject): a role-only change (builder gaining network) isn't compared by any
    of the list/scalar fields, so the old summarizer found "no changes" and hid it completely."""
    from troupe.tui.panes.needs_you import render_card

    previous = dict(safety=dict(protected=["src/troupe/safety.py"], remotes=[], secret_allow=[],
                                roles={}), check="", check_timeout=600)
    proposed = dict(safety=dict(protected=["src/troupe/safety.py"], remotes=[], secret_allow=[],
                                roles={"builder": {"network": True}}), check="", check_timeout=600)
    body = render_card(_baseline_question(previous, proposed))
    assert "no changes detected" not in body
    assert "builder" in body
    assert "network" in body
    assert "true" in body.lower()


def test_changed_baseline_card_names_the_new_remote_and_dropped_protected_path():
    """#85 round 2 (QA reject): "protected paths +0 -1; remotes +1 -0" hides exactly what a human
    approving a safety change needs to see — the actual remote and the actual path."""
    from troupe.tui.panes.needs_you import render_card

    previous = dict(safety=dict(protected=["src/troupe/safety.py", "src/troupe/roles.py"],
                                remotes=[], secret_allow=[], roles={}), check="", check_timeout=600)
    proposed = dict(safety=dict(protected=["src/troupe/roles.py"],
                                remotes=["git@github.com:attacker/exfil.git"], secret_allow=[],
                                roles={}), check="", check_timeout=600)
    body = render_card(_baseline_question(previous, proposed))
    assert "−src/troupe/safety.py" in body
    assert "+git@github.com:attacker/exfil.git" in body


def test_changed_baseline_card_falls_back_to_raw_context_for_an_unrecognized_change():
    """#85 round 2 (QA reject): the summarizer only knows protected/remotes/secret_allow/roles/
    check/check_timeout — a change to anything else must not be reported as "no changes detected"
    (approving blind), so it falls back to the same raw context main used to show."""
    from troupe.tui.panes.needs_you import render_card

    previous = dict(safety=dict(protected=["src/troupe/safety.py"], remotes=[], secret_allow=[],
                                roles={}), check="", check_timeout=600)
    proposed = dict(safety=dict(protected=["src/troupe/safety.py"], remotes=[], secret_allow=[],
                                roles={}, unrecognized_field="sneaky"), check="", check_timeout=600)
    body = render_card(_baseline_question(previous, proposed))
    assert "no changes detected" not in body
    assert "unrecognized_field" in body
    assert "sneaky" in body


def test_changed_baseline_card_shows_known_diff_and_raw_context_for_a_mixed_change():
    """#109: a proposal that mixes a known-field change (roles) with a brand-new safety.<key> the
    summarizer doesn't recognize used to show only the recognized half — the human would approve
    the unrecognized part blind. Both the known summary and the raw context must appear."""
    from troupe.tui.panes.needs_you import render_card

    previous = dict(safety=dict(protected=[], remotes=[], secret_allow=[], roles={}),
                    check="", check_timeout=600)
    proposed = dict(safety=dict(protected=[], remotes=[], secret_allow=[],
                                roles={"builder": {"network": True}}, future_field="sneaky"),
                    check="", check_timeout=600)
    body = render_card(_baseline_question(previous, proposed))
    assert "no changes detected" not in body
    assert "builder" in body and "network" in body  # known diff still summarized
    assert "other changes:" in body
    assert "future_field" in body and "sneaky" in body  # raw context still shown alongside it


def test_changed_baseline_card_shows_known_diff_and_raw_context_for_an_unrecognized_top_level_key():
    """#109: same as above, but the unrecognized field is a brand-new top-level key (outside
    `safety`) rather than a `safety.*` one — both diff-stripping paths must catch it."""
    from troupe.tui.panes.needs_you import render_card

    previous = dict(safety=dict(protected=["src/troupe/safety.py"], remotes=[], secret_allow=[],
                                roles={}), check="", check_timeout=600)
    proposed = dict(safety=dict(protected=["src/troupe/roles.py"], remotes=[], secret_allow=[],
                                roles={}), check="", check_timeout=600, future_gate="strict")
    body = render_card(_baseline_question(previous, proposed))
    assert "no changes detected" not in body
    assert "−src/troupe/safety.py" in body and "+src/troupe/roles.py" in body  # known diff
    assert "other changes:" in body
    assert "future_gate" in body and "strict" in body  # raw context still shown alongside it


def test_pinned_safety_card_renders_first_among_six_ordinary_questions():
    """#109 acceptance: newest-first ordering could push a pending safety approval off-screen below
    ordinary questions — it must be pinned to the top regardless of arrival order."""
    async def body():
        client = FakeClient([question(i) for i in range(1, 7)] + [safety_question(7)])
        async with NeedsYouTestApp(client).run_test() as pilot:
            list_view = pilot.app.query_one("#ny-cards", ListView)
            first = list_view.children[0]
            assert isinstance(first, QuestionCard)
            assert first.question["id"] == 7
            assert first.is_safety

    run(body())


def test_pinned_safety_cards_sort_oldest_first_within_their_own_group():
    """design/system.md "Pinning & priority order (#109)": the safety group is oldest-first within
    itself (a merge gate), unlike the newest-first ordering everything else in the panel uses."""
    async def body():
        older = safety_question(1)
        older["ts"] = 100
        newer = safety_question(2)
        newer["ts"] = 200
        client = FakeClient([newer, older])  # delivered newest-first, as the API would
        async with NeedsYouTestApp(client).run_test() as pilot:
            list_view = pilot.app.query_one("#ny-cards", ListView)
            ids = [item.question["id"] for item in list_view.children]
            assert ids == [1, 2]  # oldest (ts=100) first, despite arriving after the newer one

    run(body())
