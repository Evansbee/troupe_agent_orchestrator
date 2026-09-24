"""REQ-COM-025 (inbox keyboard answering) + REQ-GUI-027 (title needs-you count) regression tests.

Keyboard/window-title code paths that call raylib directly (rl.is_key_pressed, rl.set_window_title)
aren't covered here — this suite has no precedent for opening a real window (see
test_agent_view_runs.py). Instead these tests cover the pure-Python logic app.py's draw_inbox/run()
delegate to: which question 1-9 should apply to (keyboard_answer_target), what answering it does
(answer_question_option), and what the title string should say (window_title).
"""

from troupe.gui.app import App, window_title
from troupe.gui.views import answer_question_option, keyboard_answer_target


def _ask(store, asker: str = "lead", options: list[str] | None = None) -> int:
    return store.ask(asker, "Pick one?", "", options or ["Yes", "No", "Maybe"])


def test_window_title_shows_count_only_when_questions_are_open():
    assert window_title("demo", 0) == "troupe — demo"
    assert window_title("demo", 1) == "(1) troupe — demo"
    assert window_title("demo", 3) == "(3) troupe — demo"


def test_keyboard_answer_target_is_none_while_typing(project):
    """Ignored whenever a text input has focus — typing "3" in a reply must never answer a
    question."""
    cfg, store = project
    _ask(store)
    app = App(cfg)
    app.data.refresh(force=True)
    app.ui.focus = "chat:lead"

    assert keyboard_answer_target(app, hovered=None) is None


def test_keyboard_answer_target_is_none_while_cmd_is_held(project):
    """⌘1-9 is the tab-switch shortcut (App.shortcuts) — the same digits must not also answer a
    question."""
    cfg, store = project
    _ask(store)
    app = App(cfg)
    app.data.refresh(force=True)
    app.ui.cmd = True

    assert keyboard_answer_target(app, hovered=None) is None


def test_keyboard_answer_target_prefers_hovered_over_top_card(project):
    cfg, store = project
    _ask(store)
    q2_id = store.ask("pm", "Second question?", "", ["A", "B"])
    app = App(cfg)
    app.data.refresh(force=True)
    hovered = next(q for q in app.data.questions if q["id"] == q2_id)

    assert keyboard_answer_target(app, hovered) is hovered


def test_keyboard_answer_target_falls_back_to_top_card_when_nothing_hovered(project):
    cfg, store = project
    qid = _ask(store)
    app = App(cfg)
    app.data.refresh(force=True)

    target = keyboard_answer_target(app, hovered=None)
    assert target is not None and target["id"] == qid


def test_keyboard_answer_target_is_none_with_no_open_questions(project):
    cfg, store = project
    app = App(cfg)
    app.data.refresh(force=True)

    assert keyboard_answer_target(app, hovered=None) is None


def test_answer_question_option_picks_the_indexed_option_and_toasts(project):
    cfg, store = project
    qid = _ask(store, options=["Yes", "No", "Maybe"])
    app = App(cfg)
    app.data.refresh(force=True)
    q = app.data.questions[0]

    assert answer_question_option(app, q, 1) is True  # index 1 -> "No"
    row = store.q("SELECT answer, status FROM questions WHERE id=?", qid)[0]
    assert row["answer"] == "No"
    assert row["status"] == "answered"
    assert app.toasts and "Pick one?" in app.toasts[-1][1]


def test_answer_question_option_merges_free_text_extra(project):
    """Matches the option button's own behavior: whatever's already typed in the free-form reply
    box is appended to the picked option, same as clicking it."""
    cfg, store = project
    qid = _ask(store, options=["Yes", "No"])
    app = App(cfg)
    app.data.refresh(force=True)
    q = app.data.questions[0]
    app.ui.set_input(f"qa{qid}", "because reasons")

    answer_question_option(app, q, 0)
    row = store.q("SELECT answer FROM questions WHERE id=?", qid)[0]
    assert row["answer"] == "Yes — because reasons"


def test_answer_question_option_ignores_an_out_of_range_index(project):
    """A card with 2 options and the human presses "5" — must be a no-op, not a crash."""
    cfg, store = project
    qid = _ask(store, options=["Yes", "No"])
    app = App(cfg)
    app.data.refresh(force=True)
    q = app.data.questions[0]

    assert answer_question_option(app, q, 4) is False
    row = store.q("SELECT status FROM questions WHERE id=?", qid)[0]
    assert row["status"] == "open"


def test_answering_by_keyboard_removes_the_card_like_a_click(project):
    """"The answered card gets the same feedback as a click (toast + card leaves)" — after
    answering, the question drops out of Data.questions on the next refresh, same as the button."""
    cfg, store = project
    qid = _ask(store, options=["Yes", "No"])
    app = App(cfg)
    app.data.refresh(force=True)
    q = app.data.questions[0]

    answer_question_option(app, q, 0)
    assert qid not in {r["id"] for r in app.data.questions}
