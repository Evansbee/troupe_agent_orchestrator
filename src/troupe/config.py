"""Project configuration: .troupe/troupe.toml."""

from __future__ import annotations

import tomllib
import math
from dataclasses import dataclass, field
from pathlib import Path

from .roles import get_role

STATE_DIR = ".troupe"
CONFIG_FILE = "troupe.toml"


@dataclass
class AgentCfg:
    id: str
    role: str
    name: str
    backend: str  # claude | codex | local
    model: str = ""
    enabled: bool = True
    idle_minutes: float | None = None  # override role default
    effort: str = ""  # claude --effort
    extra_args: list[str] = field(default_factory=list)

    @property
    def idle_seconds(self) -> float:
        m = self.idle_minutes if self.idle_minutes is not None else get_role(self.role).idle_minutes
        return m * 60


@dataclass
class Budget:
    max_concurrent: int = 3
    max_runs_per_hour: int = 40
    max_usd_per_day: float = 0.0  # 0 = unlimited (claude-reported cost)
    max_task_attempts: int = 4


@dataclass
class Backends:
    claude_command: str = "claude"
    claude_strict_mcp: bool = True
    codex_command: str = "codex"
    local_base_url: str = "http://localhost:1234/v1"
    local_api_key: str = "lm-studio"
    local_max_steps: int = 30


@dataclass
class GitSettings:
    setup: str = ""
    check: str = ""
    check_timeout: float = 600

    def __post_init__(self) -> None:
        if not isinstance(self.setup, str) or not isinstance(self.check, str):
            raise ValueError("troupe.toml: git.setup and git.check must be strings")
        if (isinstance(self.check_timeout, bool) or not isinstance(self.check_timeout, (int, float))
                or not math.isfinite(self.check_timeout) or self.check_timeout < 0):
            raise ValueError("troupe.toml: git.check_timeout must be a non-negative number")


@dataclass
class Config:
    root: Path
    project: str
    agents: list[AgentCfg]
    budget: Budget = field(default_factory=Budget)
    backends: Backends = field(default_factory=Backends)
    git_autocommit: bool = True
    git: GitSettings = field(default_factory=GitSettings)

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR

    @property
    def db_path(self) -> Path:
        return self.state_dir / "troupe.db"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    @property
    def worktrees_dir(self) -> Path:
        return self.state_dir / "worktrees"

    def agent(self, agent_id: str) -> AgentCfg | None:
        return next((a for a in self.agents if a.id == agent_id), None)

    def agents_with_role(self, role: str) -> list[AgentCfg]:
        return [a for a in self.agents if a.role == role]


DEFAULT_TOML = """\
# troupe configuration — edit freely; restart `troupe up` to apply.

[project]
name = "{name}"

[budget]
max_concurrent = 3        # agent runs at once (a chat with the human always gets an extra slot)
max_runs_per_hour = 40    # autonomous runs per rolling hour (chat is exempt)
max_usd_per_day = 0       # claude-reported cost cap per rolling 24h; 0 = unlimited
max_task_attempts = 4     # builder sessions on one task before it's marked blocked

[backends]
claude_command = "claude"
claude_strict_mcp = true  # agents only see the troupe MCP server (faster startup)
codex_command = "codex"
local_base_url = "http://localhost:1234/v1"   # any OpenAI-compatible server (LM Studio, Ollama, vLLM)
local_api_key = "lm-studio"

[git]
autocommit = true         # commit doc/spec changes in the main tree after each non-builder run
setup = ""               # command run once in each new worktree, e.g. "uv sync"
# check = "uv run pytest" # optional merge gate; omitted/empty means no gate
check_timeout = 600      # seconds

# ── The team ─────────────────────────────────────────────────────────────
# backend: claude | codex | local.  model: backend-specific ("" = backend default).
# idle_minutes overrides how often an agent proactively looks for work (0 = never).

[[agents]]
id = "lead"
role = "lead"
name = "Lead"
backend = "claude"
model = "opus"

[[agents]]
id = "pm"
role = "pm"
name = "Product Manager"
backend = "claude"
model = "opus"

[[agents]]
id = "spec"
role = "spec"
name = "Spec Writer"
backend = "claude"
model = "opus"

[[agents]]
id = "designer"
role = "designer"
name = "Designer"
backend = "claude"
model = "sonnet"

[[agents]]
id = "builder-1"
role = "builder"
name = "Builder 1"
backend = "claude"
model = "sonnet"

[[agents]]
id = "builder-2"
role = "builder"
name = "Builder 2"
backend = "codex"
model = ""

[[agents]]
id = "qa"
role = "qa"
name = "QA"
backend = "codex"
model = ""

[[agents]]
id = "gadfly"
role = "gadfly"
name = "Gadfly"
backend = "local"
model = "{local_model}"
"""


def find_root(start: Path | None = None) -> Path | None:
    p = (start or Path.cwd()).resolve()
    for d in [p, *p.parents]:
        if (d / STATE_DIR / CONFIG_FILE).exists():
            return d
    return None


def write_default(root: Path, name: str, local_model: str = "qwen/qwen3.8-27b") -> Path:
    path = root / STATE_DIR / CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_TOML.format(name=name, local_model=local_model))
    return path


def load(root: Path) -> Config:
    raw = tomllib.loads((root / STATE_DIR / CONFIG_FILE).read_text())
    proj = raw.get("project", {})
    b = raw.get("budget", {})
    be = raw.get("backends", {})
    agents = []
    for a in raw.get("agents", []):
        get_role(a["role"])  # validate
        agents.append(AgentCfg(
            id=a["id"], role=a["role"], name=a.get("name", a["id"]), backend=a.get("backend", "claude"),
            model=a.get("model", ""), enabled=a.get("enabled", True), idle_minutes=a.get("idle_minutes"),
            effort=a.get("effort", ""), extra_args=list(a.get("extra_args", [])),
        ))
    ids = [a.id for a in agents]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate agent ids in troupe.toml")
    if sum(a.role == "lead" for a in agents) != 1:
        raise ValueError("a troupe needs exactly one agent with role = \"lead\"")
    return Config(
        root=root,
        project=proj.get("name", root.name),
        agents=agents,
        budget=Budget(**{k: v for k, v in b.items() if k in Budget.__dataclass_fields__}),
        backends=Backends(**{k: v for k, v in be.items() if k in Backends.__dataclass_fields__}),
        git_autocommit=raw.get("git", {}).get("autocommit", True),
        git=GitSettings(**{k: v for k, v in raw.get("git", {}).items() if k in GitSettings.__dataclass_fields__}),
    )
