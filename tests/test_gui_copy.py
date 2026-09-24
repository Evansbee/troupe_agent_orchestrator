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
    assert ui.copy_button(Rect(0, 0, 46, 20), False) is False


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
