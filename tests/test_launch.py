import asyncio

from troupe.engine import Engine, Wake
from troupe.runners import RunResult, Runner


class FakeRunner(Runner):
    """Records the RunSpec it was handed; optionally raises to simulate a crashed backend."""

    def __init__(self, crash: bool = False):
        super().__init__()
        self.crash = crash
        self.received_spec = None

    async def run(self, spec, emit):
        self.received_spec = spec
        if self.crash:
            raise RuntimeError("backend crashed")
        return RunResult(ok=True, final_text="done", cost=0.02, tokens=150)


def _launch(cfg, store, monkeypatch, crash: bool):
    """Drives Engine.launch() for the lead agent with a fake backend, returns (run_id, fake, row_before)."""
    fake = FakeRunner(crash=crash)
    monkeypatch.setattr("troupe.engine.make_runner", lambda backend: fake)
    engine = Engine(cfg)
    engine.store = store
    lead = next(a for a in cfg.agents if a.role == "lead")

    async def scenario():
        wake = Wake(priority=3, agent=lead, reason="task", task=None)
        await engine.launch(wake)
        run_id = store.scalar("SELECT MAX(id) FROM runs")
        # The fake backend hasn't run yet: launch() has no await before scheduling the run task,
        # so persistence here proves prompt/system are written before the backend starts (REQ-ENG-018).
        row_before = store.one("SELECT * FROM runs WHERE id=?", run_id)
        assert fake.received_spec is None
        task = engine.running[lead.id][1]
        await task
        return run_id, row_before

    run_id, row_before = asyncio.run(scenario())
    return run_id, fake, row_before


def test_launch_persists_prompt_and_system_before_backend_starts(project, monkeypatch):
    cfg, store = project
    run_id, fake, row_before = _launch(cfg, store, monkeypatch, crash=False)

    assert row_before["prompt"] and row_before["system"]
    assert fake.received_spec.prompt == row_before["prompt"]
    assert fake.received_spec.system == row_before["system"]

    row_after = store.one("SELECT * FROM runs WHERE id=?", run_id)
    assert row_after["status"] == "ok"
    assert row_after["prompt"] == row_before["prompt"]
    assert row_after["system"] == row_before["system"]


def test_launch_persisted_prompt_and_system_survive_backend_crash(project, monkeypatch):
    cfg, store = project
    run_id, fake, row_before = _launch(cfg, store, monkeypatch, crash=True)

    assert row_before["prompt"] and row_before["system"]
    assert fake.received_spec.prompt == row_before["prompt"]
    assert fake.received_spec.system == row_before["system"]

    row_after = store.one("SELECT * FROM runs WHERE id=?", run_id)
    assert row_after["status"] == "failed"
    assert row_after["prompt"] == row_before["prompt"]
    assert row_after["system"] == row_before["system"]
