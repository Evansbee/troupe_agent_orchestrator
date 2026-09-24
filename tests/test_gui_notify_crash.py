"""Regression for #90: `troupe up` crashed on every launch with `AttributeError: 'Data' object has
no attribute 'notify'` — gui/app.py:121 called a `d.notify(...)` that `gui/data.py` never defined.
It fired on the first `needs_help` message to the human, which the safety-baseline approval card
always is on a fresh install.

QA's caveat (msg #813): a plain `App(cfg); refresh(); handle_notifications()` doesn't reproduce it,
because gui/data.py's `new_messages` only contains messages newer than the *first* refresh's
snapshot — a message already pending before that first refresh is baseline, not new. The real crash
came from the message landing *after* the GUI's first refresh (the engine delivers the safety card
shortly after `troupe up` starts both processes), so the repro here matches that: refresh once to
establish the baseline, insert the message the way safety.py does, then refresh + handle again.
"""
from troupe.gui.app import App


def test_needs_help_message_after_first_refresh_does_not_crash(project):
    cfg, store = project
    app = App(cfg)
    app.data.refresh(force=True)  # baseline snapshot, as if the GUI just opened

    store.send("system", "human", "Safety baseline needs your approval.",
              subject="Safety needs attention", kind="needs_help")

    app.data.refresh(force=True)  # picks up the message as "new", the path that crashed on main
    app.handle_notifications()  # must not raise AttributeError: 'Data' object has no attribute 'notify'

    assert any("Safety baseline needs your approval" in t[1] for t in app.toasts)
