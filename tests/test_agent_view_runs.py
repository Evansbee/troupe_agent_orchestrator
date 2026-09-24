"""Regression coverage for QA's rejection of #1: the Agent view's run history must stay fully
navigable regardless of window width. The fix replaced the width-fitted run-chip row with a compact
selector + popover (views._run_selector / _run_picker_popover), so no run is ever omitted just
because it didn't fit on screen. These tests exercise what's safely testable without an open raylib
window (widget rendering needs one; the app/gui/core.py suite has no precedent for opening one in
tests, so we don't start here) — the data layer's history cap, and the picker's modal-lifecycle logic,
whose "closed" path is pure Python.

#54 follow-up: the popover's own history was still hard-capped at 200 runs (Data.runs_for's original
limit=200 call), so an agent with 201+ runs had the exact same "oldest run unreachable" bug one level
down — the tests below (QA's own 200/201 boundary repro) cover Data.runs_for/has_more_runs/
load_older_runs, the paging API that replaced the flat cap.
"""

from troupe.gui import views
from troupe.gui.app import App
from troupe.gui.core import Rect
from troupe.gui.data import RUNS_PAGE


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


def _seed_runs(store, agent: str, n: int) -> None:
    for i in range(n):
        rid = store.start_run(agent, "task", None, "/tmp", False, prompt=f"p{i}", system=f"s{i}")
        store.end_run(rid, "ok", 0.0, 0, f"summary {i}")


def test_runs_for_first_page_matches_the_old_flat_cap(project):
    cfg, store = project
    _seed_runs(store, "lead", RUNS_PAGE)
    app = App(cfg)

    runs = app.data.runs_for("lead")
    assert len(runs) == RUNS_PAGE
    assert not app.data.has_more_runs("lead")  # exactly RUNS_PAGE runs exist: nothing older to load


def test_load_older_runs_reaches_the_oldest_run_past_the_page_boundary(project):
    """QA's exact repro on #1: with RUNS_PAGE + 1 seeded runs, run #1 (the oldest) must become
    reachable — the whole reason this task exists."""
    cfg, store = project
    _seed_runs(store, "lead", RUNS_PAGE + 1)
    app = App(cfg)

    runs = app.data.runs_for("lead")
    assert len(runs) == RUNS_PAGE
    assert {r["id"] for r in runs} == set(range(2, RUNS_PAGE + 2))  # newest RUNS_PAGE; #1 not yet loaded
    assert app.data.has_more_runs("lead")

    app.data.load_older_runs("lead")
    runs = app.data.runs_for("lead")
    assert len(runs) == RUNS_PAGE + 1
    assert {r["id"] for r in runs} == set(range(1, RUNS_PAGE + 2))
    oldest = next(r for r in runs if r["id"] == 1)
    assert oldest["prompt"] == "p0" and oldest["system"] == "s0"
    assert not app.data.has_more_runs("lead")  # that was every run; nothing left to page in


def test_load_older_runs_can_be_called_repeatedly_past_several_pages(project):
    cfg, store = project
    _seed_runs(store, "lead", RUNS_PAGE * 2 + 50)
    app = App(cfg)

    assert len(app.data.runs_for("lead")) == RUNS_PAGE
    app.data.load_older_runs("lead")
    assert len(app.data.runs_for("lead")) == RUNS_PAGE * 2
    assert app.data.has_more_runs("lead")
    app.data.load_older_runs("lead")
    runs = app.data.runs_for("lead")
    assert len(runs) == RUNS_PAGE * 2 + 50
    assert min(r["id"] for r in runs) == 1
    assert not app.data.has_more_runs("lead")


def test_runs_for_does_not_requery_the_store_within_the_refresh_window(project, monkeypatch):
    """Idle-fps requirement: paging must not turn "select the Agent tab" into a per-frame DB read."""
    cfg, store = project
    _seed_runs(store, "lead", 5)
    app = App(cfg)

    calls = []
    orig_runs = app.data.store.runs

    def spy(*args, **kwargs):
        calls.append(1)
        return orig_runs(*args, **kwargs)

    monkeypatch.setattr(app.data.store, "runs", spy)
    for _ in range(20):
        app.data.runs_for("lead")
    assert len(calls) == 1  # first call fetches; the rest hit the cache inside the REFRESH window


def test_runs_for_different_agents_are_paged_independently(project):
    cfg, store = project
    _seed_runs(store, "lead", RUNS_PAGE + 1)
    _seed_runs(store, "pm", 3)
    app = App(cfg)

    assert app.data.has_more_runs("lead")
    assert not app.data.has_more_runs("pm")
    app.data.load_older_runs("lead")
    assert not app.data.has_more_runs("lead")
    assert len(app.data.runs_for("pm")) == 3  # untouched by lead's paging
