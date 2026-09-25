"""REQ-ENG-070: the Pulse tab's "$ Spend" toggle. `_spend_report` is the pure, cacheable half of
that feature (see AGENTS.md on never doing blocking work in draw code) -- it reads the GUI's own
Store handle directly (no socket, unlike the TUI) and is exercised here without opening a window."""
from troupe.gui.app import App
from troupe.gui.views import SPEND_REFRESH, PulseState, _spend_report


def _run(store, agent, cost=1.0, tokens=100):
    rid = store.start_run(agent, "task", None, "/tmp", False)
    store.run_line(rid, "tool", "edit foo.py")
    store.end_run(rid, "ok", cost, tokens, "done")


def test_spend_report_reflects_store_data(project):
    cfg, store = project
    _run(store, "builder-1", cost=3.5, tokens=400)
    app = App(cfg)
    p = PulseState()

    lines = _spend_report(app, p)
    text = "\n".join(lines)
    assert "$3.50" in text
    assert "builder-1" in text or "builder_1" in text  # name_of resolves to a display handle


def test_spend_report_is_cached_until_refresh_window_elapses(project, monkeypatch):
    cfg, store = project
    _run(store, "builder-1", cost=1.0, tokens=100)
    app = App(cfg)
    p = PulseState()

    first = _spend_report(app, p)
    _run(store, "builder-1", cost=5.0, tokens=500)  # a second run lands...
    second = _spend_report(app, p)
    assert second == first  # ...but isn't reflected until the cache expires

    p.spend_cache_at -= SPEND_REFRESH + 1  # simulate time passing
    third = _spend_report(app, p)
    assert "$6.00" in "\n".join(third)
