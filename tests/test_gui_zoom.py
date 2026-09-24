from troupe.gui.app import App
from troupe.gui.core import ZOOM_DEFAULT, ZOOM_MAX, ZOOM_MIN, ZOOM_STEP, UI


def test_default_zoom_is_a_bit_larger_than_unzoomed():
    assert ZOOM_MIN <= ZOOM_DEFAULT <= ZOOM_MAX
    assert ZOOM_DEFAULT > 1.0


def test_set_zoom_clamps_to_range():
    ui = UI()
    ui.set_zoom(99)
    assert ui.zoom == ZOOM_MAX
    ui.set_zoom(-5)
    assert ui.zoom == ZOOM_MIN


def test_set_zoom_snaps_to_the_step_grid():
    ui = UI()
    ui.set_zoom(1.088)
    assert ui.zoom == 1.10
    ui.set_zoom(0.83)
    assert ui.zoom == 0.85


def test_set_zoom_is_a_noop_at_the_same_value():
    ui = UI()
    base = ZOOM_MIN + ZOOM_STEP
    ui.set_zoom(base)
    assert ui.set_zoom(base) is False
    assert ui.set_zoom(base + ZOOM_STEP) is True


def test_zoom_in_out_reset_cycle_matches_app_shortcuts():
    ui = UI()
    ui.set_zoom(ZOOM_DEFAULT)
    ui.set_zoom(ui.zoom + ZOOM_STEP)
    assert round(ui.zoom, 2) == round(ZOOM_DEFAULT + ZOOM_STEP, 2)
    ui.set_zoom(ZOOM_DEFAULT)
    assert ui.zoom == ZOOM_DEFAULT


def test_app_set_zoom_persists_to_kv_and_toasts(project):
    cfg, store = project
    app = App(cfg)
    assert store.kv_get("gui_zoom") is None  # nothing saved yet

    app.set_zoom(ZOOM_DEFAULT + ZOOM_STEP)

    assert store.kv_get("gui_zoom") == round(ZOOM_DEFAULT + ZOOM_STEP, 2)
    assert app.toasts and "Zoom" in app.toasts[-1][1]


def test_app_set_zoom_reload_restores_persisted_value(project):
    cfg, store = project
    first = App(cfg)
    first.set_zoom(0.85)

    second = App(cfg)
    second.ui.set_zoom(store.kv_get("gui_zoom", ZOOM_DEFAULT))  # what App.run() does at startup
    assert second.ui.zoom == 0.85


def test_app_set_zoom_noop_does_not_toast_or_write_kv(project):
    cfg, store = project
    app = App(cfg)
    app.ui.zoom = ZOOM_MAX
    app.set_zoom(ZOOM_MAX)  # already at this value
    assert store.kv_get("gui_zoom") is None
    assert app.toasts == []
