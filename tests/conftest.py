import pytest

from troupe import config, gitops
from troupe.store import Store


@pytest.fixture
def project(tmp_path):
    """A fresh troupe project with the default team, in a git repo."""
    config.write_default(tmp_path, "test-project")
    gitops.ensure_repo(tmp_path)
    cfg = config.load(tmp_path)
    store = Store(cfg.db_path)
    store.sync_agents(cfg.agents)
    return cfg, store
