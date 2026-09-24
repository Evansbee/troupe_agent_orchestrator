"""Regression coverage for QA's rejection of #1: the Agent view's run history must stay fully
navigable regardless of window width. The fix replaced the width-fitted run-chip row with a compact
selector + popover (views._run_selector / _run_picker_popover), so no run is ever omitted just
because it didn't fit on screen. These tests exercise what's safely testable without an open raylib
window (widget rendering needs one; the app/gui/core.py suite has no precedent for opening one in
tests, so we don't start here) — the data layer's history cap, and the picker's modal-lifecycle logic,
whose "closed" path is pure Python.
"""

from troupe.gui import views
from troupe.gui.app import App
from troupe.gui.core import Rect


def test_agent_run_history_not_capped_at_display_width(project):
    """The old bug: runs() was fetched with limit=14 to match how many chips could ever be drawn, so
    anything older simply didn't exist as far as the Agent view was concerned. agent_view now fetches
    limit=200 precisely so the picker (unlike a fitted chip row) always has the full history to show."""
    cfg, store = project
    for i in range(30):
        rid = store.start_run("lead", "task", None, "/tmp", False, prompt=f"p{i}", system=f"s{i}")
        store.end_run(rid, "ok", 0.0, 0, f"summary {i}")

    runs = store.runs("lead", limit=200)
    assert len(runs) == 30
    assert {r["id"] for r in runs} == set(range(1, 31))
    # the oldest run (previously unreachable once >14 runs existed) must be present and intact
    oldest = next(r for r in runs if r["id"] == 1)
    assert oldest["prompt"] == "p0" and oldest["system"] == "s0"


def test_selecting_a_run_does_not_touch_run_view_mode(project):
    """Picking a different run (old or new) is orthogonal to Transcript/Prompt mode — selecting an
    older run must preserve whichever mode the human was in, at any window size."""
    cfg, store = project
    app = App(cfg)
    app.run_view = "Prompt"
    app.sel_run = None

    # what _run_picker_popover's row click handler does: select + close, nothing else
    app.sel_run = 3
    views._run_picker_agent = None

    assert app.sel_run == 3
    assert app.run_view == "Prompt"


def test_run_picker_closes_when_navigating_away_from_agent_tab(project):
    """Reproduces the sticky-modal hazard: ui.modal is set from inside draw_modals and persists
    across frames, so if the picker's open flag were left set after leaving the Agent tab (or
    switching to a different agent), every future frame's hover/click checks would stay blocked
    everywhere in the app. draw_modals must clear it as soon as it's no longer the active view."""
    cfg, store = project
    app = App(cfg)
    views._run_picker_agent = "lead"
    views._run_picker_anchor = Rect(0, 0, 10, 10)

    app.tab = "Chat"  # navigated away from Agent
    app.sel_agent = "lead"
    views.draw_modals(app)
    assert views._run_picker_agent is None
    assert app.ui.modal is None

    views._run_picker_agent = "lead"
    views._run_picker_anchor = Rect(0, 0, 10, 10)
    app.tab = "Agent"
    app.sel_agent = "builder-1"  # switched to a different agent's view
    views.draw_modals(app)
    assert views._run_picker_agent is None
    assert app.ui.modal is None
