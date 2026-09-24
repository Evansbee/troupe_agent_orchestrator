import pytest

from troupe import config, gitops
from troupe.store import Store


@pytest.fixture
def project(tmp_path):
    """A fresh troupe project with the default team, in a git repo."""
    state = tmp_path / config.STATE_DIR
    state.mkdir()
    (state / config.CONFIG_FILE).write_text(config.DEFAULT_TOML.format(name="test-project", local_model="local-test"))
    gitops.ensure_repo(tmp_path)
    cfg = config.load(tmp_path)
    store = Store(cfg.db_path)
    baseline = store.kv_get("safety.config")
    store.answer(baseline["qid"], "Approve")
    cfg = config.load(tmp_path)
    # Tests start after human onboarding, with an empty activity/mail inbox.
    store.x("DELETE FROM messages")
    store.x("DELETE FROM events")
    store.sync_agents(cfg.agents)
    return cfg, store
