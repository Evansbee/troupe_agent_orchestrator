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
"""

from troupe.gui.core import ScrollState, _re_stick, _scroll_apply_drag, _scroll_apply_wheel, _scroll_commit_content


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
    assert sc.target == sc.seen == sc.content - sc.view


def test_a_fresh_scroll_state_for_a_new_chat_partner_starts_stuck_at_the_bottom():
    """Switching chat partners uses a per-agent scroll id, so it's always a brand-new ScrollState —
    the dataclass default (at_bottom=True) already means "starts at the bottom"."""
    sc = ScrollState(stick=True, view=200.0)
    assert sc.at_bottom
    _scroll_commit_content(sc, 900, max(0.0, 900 - sc.view))
    assert sc.at_bottom
    assert sc.offset == 900 - sc.view
