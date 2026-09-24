"""REQ-GUI-017: stick-to-bottom scrolling must not lose its grip when content grows, only when the
human actually scrolls away. The bug: scroll_end used to recompute `at_bottom` from `target >=
max_off - 4` using content_h's *new* max_off against a `target` set one call earlier against the
*old* max_off — any single-frame content growth bigger than the 4px threshold (a streaming reply, a
long message) silently flipped at_bottom to False.

core.py's scroll_begin/scroll_end call raylib's scissor-mode functions (push_clip/pop_clip), which
segfault without an open window — this suite has no precedent for opening one in tests (see
test_agent_view_runs.py), so these tests exercise the pure functions the fix extracted:
_scroll_apply_wheel, _scroll_apply_drag, _scroll_commit_content, _re_stick. Together they cover
every place ScrollState.at_bottom can change, which is exactly the logic under test. The visual
pieces (the "New messages" pill's rendering, scrollbar drag hit-testing) are verified separately by
screenshot, per this task's own acceptance criteria.

QA rejected the first pass (msg #324): a per-agent/per-run ScrollState persists in ui.scrolls once
created, so its dataclass at_bottom=True default only ever covers the *first* time an agent's chat
(or a run's transcript) is viewed — revisiting a partner/run seen earlier the same session, or
leaving the Chat/Agent tab and coming straight back, left it exactly as scrolled-up as before. The
second half of this file covers the fix: views.chat_entered()/transcript_entered() (pure) plus
views.draw_modals() clearing the tracked agent/run whenever the tab changes away.
"""

from troupe.gui import views
from troupe.gui.app import App
from troupe.gui.core import ScrollState, _re_stick, _scroll_apply_drag, _scroll_apply_wheel, _scroll_commit_content
from troupe.gui.views import chat_entered, transcript_entered


def _stuck_at_bottom(content: float = 1000.0, view: float = 200.0) -> ScrollState:
    """A stick_bottom scroll area that has already caught up to `content`, viewport `view` tall."""
    sc = ScrollState(stick=True, view=view)
    _scroll_commit_content(sc, content, max(0.0, content - view))
    assert sc.at_bottom
    return sc


def test_content_growing_a_lot_in_one_frame_stays_pinned_to_the_bottom():
    """The exact bug repro from the task: content grows by 500px in a single frame → still stuck."""
    sc = _stuck_at_bottom(content=1000)
    _scroll_commit_content(sc, 1500, max(0.0, 1500 - sc.view))
    assert sc.at_bottom
    assert sc.offset == sc.target == 1500 - sc.view  # snapped to the *new* bottom this same frame


def test_content_growing_repeatedly_never_loses_the_grip():
    """A streaming reply that grows every frame — matches "the working bubble growing" scenario."""
    sc = _stuck_at_bottom(content=0)
    h = 0.0
    for _ in range(50):
        h += 37  # arbitrary per-frame growth, well past the old 4px threshold
        _scroll_commit_content(sc, h, max(0.0, h - sc.view))
        assert sc.at_bottom, f"lost stickiness at content height {h}"
    assert sc.offset == sc.target == h - sc.view


def test_scrolling_up_with_the_wheel_unsticks():
    sc = _stuck_at_bottom(content=1000)
    max_off = max(0.0, sc.content - sc.view)
    _scroll_apply_wheel(sc, wheel=3.0, max_off=max_off)  # positive wheel scrolls target back (up)
    assert not sc.at_bottom


def test_content_arriving_while_scrolled_up_does_not_re_stick_or_jump():
    """The "instead of jumping" half of AC2: once unstuck, new content must not silently pull the
    view back to the bottom — only an explicit user action (wheel back down, drag, pill click, or
    send) may do that."""
    sc = _stuck_at_bottom(content=1000)
    max_off = max(0.0, sc.content - sc.view)
    _scroll_apply_wheel(sc, wheel=3.0, max_off=max_off)
    assert not sc.at_bottom
    stalled_target = sc.target

    _scroll_commit_content(sc, 1800, max(0.0, 1800 - sc.view))  # more content arrives
    assert not sc.at_bottom
    assert sc.target == stalled_target  # view did not jump to the new bottom


def test_scrolling_back_down_to_the_bottom_with_the_wheel_re_sticks():
    sc = _stuck_at_bottom(content=1000)
    max_off = max(0.0, sc.content - sc.view)
    _scroll_apply_wheel(sc, wheel=3.0, max_off=max_off)
    assert not sc.at_bottom

    _scroll_apply_wheel(sc, wheel=-3.0, max_off=max_off)  # scroll back down toward the bottom
    assert sc.at_bottom


def test_dragging_the_scrollbar_away_from_and_back_to_the_bottom():
    sc = _stuck_at_bottom(content=1000)
    max_off = max(0.0, sc.content - sc.view)

    _scroll_apply_drag(sc, rel=0.2, max_off=max_off)  # drag thumb up toward the top
    assert not sc.at_bottom

    _scroll_apply_drag(sc, rel=1.0, max_off=max_off)  # drag thumb back to the bottom
    assert sc.at_bottom


def test_new_content_pill_condition_is_true_only_once_unstuck_and_new_content_arrived():
    """The pill (core.UI.new_content_pill) shows for `sc.stick and not sc.at_bottom and sc.content >
    sc.seen + 1` — this test locks in that condition against the ScrollState sequence a real session
    produces, without needing to render anything."""
    sc = _stuck_at_bottom(content=1000)

    def pill_visible(sc: ScrollState) -> bool:
        return sc.stick and not sc.at_bottom and sc.content > sc.seen + 1

    assert not pill_visible(sc)  # still stuck: no pill

    max_off = max(0.0, sc.content - sc.view)
    _scroll_apply_wheel(sc, wheel=3.0, max_off=max_off)
    assert not pill_visible(sc)  # scrolled away, but nothing new has arrived yet

    _scroll_commit_content(sc, 1400, max(0.0, 1400 - sc.view))
    assert pill_visible(sc)  # unstuck *and* new content: pill shows


def test_re_stick_jumps_to_the_bottom_and_clears_the_pill():
    """What sending a message, switching partners, or clicking the pill all do (scroll_to_bottom /
    _re_stick)."""
    sc = _stuck_at_bottom(content=1000)
    max_off = max(0.0, sc.content - sc.view)
    _scroll_apply_wheel(sc, wheel=3.0, max_off=max_off)
    _scroll_commit_content(sc, 1400, max(0.0, 1400 - sc.view))
    assert not sc.at_bottom and sc.content > sc.seen

    _re_stick(sc)
    assert sc.at_bottom
    assert sc.target == sc.content - sc.view  # scrolled to the true bottom
    assert sc.seen == sc.content  # and "caught up" tracks content height, not max_off (off-by-`view`
    # here previously meant the pill could immediately reappear on the next scroll-away, falsely)


def test_a_fresh_scroll_state_for_a_never_before_seen_chat_partner_starts_stuck_at_the_bottom():
    """The *first ever* view of a given agent's chat gets a brand-new ScrollState — the dataclass
    default (at_bottom=True) already means "starts at the bottom". This does NOT cover revisiting a
    partner seen earlier this session, whose ScrollState persists in ui.scrolls: that needs the
    explicit chat_entered()/draw_modals mechanism below (QA's #33 rejection)."""
    sc = ScrollState(stick=True, view=200.0)
    assert sc.at_bottom
    _scroll_commit_content(sc, 900, max(0.0, 900 - sc.view))
    assert sc.at_bottom
    assert sc.offset == 900 - sc.view


# ── re-sticking on arrival: revisiting a partner/run, or the tab itself (QA's #33 rejection) ──────
# chat_view/agent_view only see "did the agent/run id change this frame" — leaving the tab and
# coming back to the *same* agent/run needs a separate signal, since neither draw function runs
# while the tab is elsewhere. chat_entered()/transcript_entered() (pure, no raylib) are the
# testable half of that fix; draw_modals (called every frame regardless of tab, already exercised
# headlessly by the sticky-modal tests above) is the half that notices the tab changed.


def test_chat_entered_is_true_on_first_view_of_an_agent(project):
    cfg, store = project
    app = App(cfg)
    assert chat_entered(app, "lead") is True
    assert app._chat_open_agent == "lead"


def test_chat_entered_is_false_on_repeated_frames_for_the_same_agent(project):
    """The steady-state case while just sitting on one agent's chat — must not re-fire every frame,
    or it would fight the user's own scrolling by re-sticking constantly."""
    cfg, store = project
    app = App(cfg)
    assert chat_entered(app, "lead") is True
    assert chat_entered(app, "lead") is False
    assert chat_entered(app, "lead") is False


def test_chat_entered_is_true_again_after_leaving_and_returning_to_the_same_agent(project):
    """QA's exact #33 repro: show A, (scroll up,) switch to B, switch back to A — must re-fire for A
    the second time too, not just the first. draw_modals is what actually clears _chat_open_agent
    when the tab changes; here we simulate that directly since we can't render a real frame."""
    cfg, store = project
    app = App(cfg)
    assert chat_entered(app, "lead") is True
    assert chat_entered(app, "pm") is True  # switched partner while still on Chat
    assert chat_entered(app, "lead") is True  # switched back — must fire again, not stay False


def test_draw_modals_clears_the_tracked_chat_agent_when_the_tab_changes(project):
    """Leaving the Chat tab for a *different* agent's chat isn't the only way to go stale — leaving
    the tab entirely (e.g. to Board) and coming straight back to the *same* agent must also
    re-stick, which requires _chat_open_agent to have been cleared while away."""
    cfg, store = project
    app = App(cfg)
    app.tab = "Chat"
    assert chat_entered(app, "lead") is True
    assert chat_entered(app, "lead") is False  # steady state on Chat

    app.tab = "Board"
    views.draw_modals(app)
    assert app._chat_open_agent is None

    app.tab = "Chat"
    assert chat_entered(app, "lead") is True  # back on the same agent: fires again


def test_transcript_entered_mirrors_chat_entered_for_runs(project):
    cfg, store = project
    app = App(cfg)
    assert transcript_entered(app, 1) is True
    assert transcript_entered(app, 1) is False
    assert transcript_entered(app, 2) is True  # different run selected
    assert transcript_entered(app, 1) is True  # back to run #1: fires again


def test_draw_modals_clears_the_tracked_run_when_leaving_the_agent_tab(project):
    cfg, store = project
    app = App(cfg)
    app.tab = "Agent"
    assert transcript_entered(app, 1) is True
    assert transcript_entered(app, 1) is False

    app.tab = "Chat"
    views.draw_modals(app)
    assert app._transcript_open_run is None

    app.tab = "Agent"
    assert transcript_entered(app, 1) is True  # same run, but re-entering the tab: fires again


def test_re_entering_a_previously_scrolled_up_chat_returns_to_the_bottom_and_sticky():
    """End-to-end at the ScrollState level: A is scrolled up, B is viewed, then A is revisited — the
    combination chat_view actually performs (chat_entered() gating a scroll_to_bottom-equivalent
    re-stick) must leave A's ScrollState pinned to the bottom and sticky again, matching QA's
    /tmp/qa33-return.png repro (old content + the pill still showing)."""
    sc_a = _stuck_at_bottom(content=3000)
    max_off = max(0.0, sc_a.content - sc_a.view)
    _scroll_apply_wheel(sc_a, wheel=5.0, max_off=max_off)  # human scrolls up while reading A
    assert not sc_a.at_bottom
    stalled_target = sc_a.target

    # more history arrives for A while the human is looking at B (does not jump — already covered
    # above, re-asserted here since it sets up the "old content" half of the repro)
    _scroll_commit_content(sc_a, 3600, max(0.0, 3600 - sc_a.view))
    assert sc_a.target == stalled_target

    # what chat_view now does on re-arrival at A: chat_entered() returns True -> scroll_to_bottom()
    _re_stick(sc_a)
    assert sc_a.at_bottom
    assert sc_a.target == sc_a.content - sc_a.view  # at the true, current bottom — not the stale one
    assert sc_a.content <= sc_a.seen + 1  # pill condition false again: no lingering "New messages"
