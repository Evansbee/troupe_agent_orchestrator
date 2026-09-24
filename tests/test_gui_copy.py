"""Coverage for #7 (hover Copy buttons + toast). Widget rendering needs an open raylib window (no
precedent for that in this suite, and it'd risk breaking on headless CI), so these stick to what's
safely testable without one: the "not shown" short-circuit (never touches a draw call), and the
clipboard/toast state machine in core.py + views.py's `_copy`/`_drain_copy_toast` helpers.
"""

from troupe.gui import views
from troupe.gui.app import App
from troupe.gui.core import UI, Rect


def test_copy_button_returns_false_and_draws_nothing_when_not_shown():
    ui = UI()
    # far from the default mouse position (0, 0) so hover(r) is also False, not just `show` —
    # otherwise copy_button would (correctly, per copy_button_active) fall through to drawing,
    # which needs an open window this suite deliberately doesn't open.
    assert ui.copy_button(Rect(500, 500, 46, 20), False) is False


def test_copy_button_active_when_parent_is_hovered():
    ui = UI()
    assert ui.copy_button_active(Rect(500, 500, 46, 20), show=True) is True


def test_copy_button_active_when_neither_parent_nor_button_is_hovered():
    ui = UI()
    ui.mouse = (0.0, 0.0)
    assert ui.copy_button_active(Rect(500, 500, 46, 20), show=False) is False


def test_copy_button_stays_active_when_pointer_moves_off_the_trigger_onto_a_disjoint_button():
    """Regression for #53: the transcript tool-chip's Copy button sits a few px outside the chip
    it's triggered by. As the pointer moves from the chip onto the (disjoint) button, `show` — the
    chip's own hover state — goes False, but the button must stay active because the pointer is now
    directly over it."""
    ui = UI()
    trigger = Rect(0, 0, 40, 20)
    button = Rect(46, 0, 46, 20)  # disjoint from trigger, mirroring the real tr.r+6 layout

    ui.mouse = (20.0, 10.0)  # over the trigger, not the button
    show = ui.hover(trigger)
    assert show is True
    assert ui.copy_button_active(button, show) is True

    ui.mouse = (60.0, 10.0)  # moved onto the button; trigger no longer hovered
    show = ui.hover(trigger)
    assert show is False
    assert ui.copy_button_active(button, show) is True  # stays active — this is the bug QA found

    ui.mouse = (200.0, 200.0)  # moved off both entirely
    show = ui.hover(trigger)
    assert ui.copy_button_active(button, show) is False


def test_copy_sets_clipboard_flag():
    ui = UI()
    assert ui.copied_text is None
    ui.copy("hello world")
    assert ui.copied_text == "hello world"


def test_copy_helper_toasts_and_clears_copied_text(project):
    cfg, store = project
    app = App(cfg)
    views._copy(app, "some text")
    assert app.ui.copied_text is None  # drained immediately, so a later markdown() check can't re-fire
    assert app.toasts and "Copied" in app.toasts[-1][1]


def test_drain_copy_toast_only_fires_when_something_was_copied(project):
    cfg, store = project
    app = App(cfg)

    views._drain_copy_toast(app)  # nothing copied yet
    assert app.toasts == []

    app.ui.copied_text = "code block contents"
    views._drain_copy_toast(app)
    assert app.toasts and "Copied" in app.toasts[-1][1]
    assert app.ui.copied_text is None


def test_drain_copy_toast_does_not_double_fire_across_two_markdown_calls(project):
    """A widget-based copy_button() click on one element must not cause a *second*, unrelated
    ui.markdown() call elsewhere in the same frame to also show a toast."""
    cfg, store = project
    app = App(cfg)

    views._copy(app, "widget copy")  # e.g. a bubble's own Copy button
    views._drain_copy_toast(app)  # a later markdown() call in the same frame checking for its own copy
    assert len(app.toasts) == 1
