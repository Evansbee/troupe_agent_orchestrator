"""Project configuration: .troupe/troupe.toml."""

from __future__ import annotations

import tomllib
import math
import os
import tempfile
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from dataclasses import dataclass, field
from pathlib import Path

from .triage import TriageSettings
from .roles import get_role

STATE_DIR = ".troupe"
CONFIG_FILE = "troupe.toml"
TEAM_FILE = "team.yaml"


@dataclass
class ProviderCfg:
    provider: str
    model: str = ""
    level: str = ""


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

    providers: list[ProviderCfg] = field(default_factory=list)

    @property
    def level(self) -> str:
        return self.effort

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
    claude_cap_percent: float = 50  # REQ-BE-016: fallback cap when a per-window one below is unset
    claude_cap_5h_percent: float | None = None  # pause new autonomous claude runs at/above this 5h
    # utilization; 0 = off; unset (None) falls back to claude_cap_percent
    claude_cap_7d_percent: float | None = None  # same, for the 7d window


CLAUDE_WINDOW_CAP_FIELDS = {"five_hour": "claude_cap_5h_percent", "seven_day": "claude_cap_7d_percent"}


def claude_window_cap(budget: "Budget", window_key: str) -> float:
    """REQ-BE-016: the effective cap percent for one claude usage window (`five_hour`/`seven_day`,
    per REQ-BE-011's naming) — that window's own cap if set, else the shared fallback. Shared by
    engine.py (the gate) and api.py (the snapshot) so both agree on what "the cap" means."""
    field = CLAUDE_WINDOW_CAP_FIELDS.get(window_key)
    specific = getattr(budget, field, None) if field else None
    return budget.claude_cap_percent if specific is None else specific


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
class NotifySettings:
    enabled: bool = True
    quiet: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        from .notify import KINDS
        if not isinstance(self.enabled, bool):
            raise ValueError('troupe.toml: notify.enabled must be boolean')
        if not isinstance(self.quiet, list) or any(not isinstance(k, str) or k not in KINDS for k in self.quiet):
            raise ValueError('troupe.toml: notify.quiet must list known event kinds: ' + ', '.join(sorted(KINDS)))


@dataclass
class Config:
    root: Path
    project: str
    agents: list[AgentCfg]
    budget: Budget = field(default_factory=Budget)
    backends: Backends = field(default_factory=Backends)
    git_autocommit: bool = True
    provider_limits: dict = field(default_factory=dict)
    team_data: dict = field(default_factory=dict, repr=False)
    toml_data: dict = field(default_factory=dict, repr=False)
    git: GitSettings = field(default_factory=GitSettings)
    triage: TriageSettings = field(default_factory=TriageSettings)
    safety: dict = field(default_factory=dict)
    notify: NotifySettings = field(default_factory=NotifySettings)

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
# troupe configuration — edits here and in team.yaml reload automatically.

[project]
name = "{name}"

[budget]
max_concurrent = 3        # agent runs at once (a chat with the human always gets an extra slot)
max_runs_per_hour = 40    # autonomous runs per rolling hour (chat is exempt)
max_usd_per_day = 0       # claude-reported cost cap per rolling 24h; 0 = unlimited
max_task_attempts = 4     # builder sessions on one task before it's marked blocked
claude_cap_percent = 50   # fallback cap for whichever of the two below is left unset
# claude_cap_5h_percent = 80  # pause new autonomous claude runs at/above this 5h utilization; 0 = off
# claude_cap_7d_percent = 50  # same, for the 7d utilization; unset = claude_cap_percent applies

[backends]
claude_command = "claude"
claude_strict_mcp = true  # agents only see the troupe MCP server (faster startup)
codex_command = "codex"
local_base_url = "http://localhost:1234/v1"   # any OpenAI-compatible server (LM Studio, Ollama, vLLM)
local_api_key = "lm-studio"

[notify]
enabled = true
quiet = []              # question, blocked, check_failed, rate_limit, providers, backoff, crash_loop, throttle, safety, chat

[git]
autocommit = true         # commit doc/spec changes in the main tree after each non-builder run
setup = ""               # command run once in each new worktree, e.g. "uv sync"
# check = "uv run pytest" # optional merge gate; omitted/empty means no gate
check_timeout = 600      # seconds

# ── The team ─────────────────────────────────────────────────────────────
# backend: claude | codex | local.  model: backend-specific ("" = backend default).
# idle_minutes overrides how often an agent proactively looks for work (0 = never).

[triage]
enabled = false
model = ""  # empty uses the configured local agent model
max_pending = 5
timeout = 5.0

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


def yaml_codec() -> YAML:
    codec = YAML()
    codec.indent(mapping=2, sequence=4, offset=2)
    return codec


def save_team(root: Path, document: dict, *, overwrite: bool = True) -> None:
    """Atomically save a round-trip YAML document, retaining its comments."""
    target = root / STATE_DIR / TEAM_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
        yaml_codec().dump(document, stream)
    try:
        if overwrite:
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
    finally:
        temporary.unlink(missing_ok=True)


def default_team(local_model: str | None) -> dict:
    agents = CommentedSeq()
    for role, count in (("lead", 1), ("pm", 1), ("spec", 1), ("designer", 1), ("builder", 2),
                        ("qa", 1), ("gadfly", 1)):
        level = "medium" if role in ("designer", "researcher", "gadfly") else "high"
        codex = {"provider": "codex", "model": "", "level": level}
        claude = {"provider": "claude", "model": "sonnet" if role in ("designer", "builder") else "opus", "level": level}
        local = {"provider": "local", "model": local_model or "", "level": ""}
        if role in ("researcher", "gadfly"):
            providers = [local, codex] if local_model else [codex]
        elif role in ("pm", "spec", "builder"):
            providers = [codex, claude] + ([local] if role == "builder" and local_model else [])
        else:
            providers = [claude, codex]
        for number in range(1, count + 1):
            row = CommentedMap(id=f"{role}_{number}", role=role, providers=[dict(p) for p in providers])
            if not local_model and role in ("builder", "researcher", "gadfly"):
                row.yaml_set_comment_before_after_key("providers", before=
                    "Local server unavailable. To enable local, insert this entry in preference order:\n"
                    "- {provider: local, model: YOUR_MODEL, level: ''}")
            agents.append(row)
    return CommentedMap(agents=agents, provider_limits={"claude": {"five_hour": 50, "seven_day": 50}})


def write_default(root: Path, name: str, local_model: str | None = None) -> Path:
    path = root / STATE_DIR / CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    # The legacy roster stays available for migration fixtures, never in new projects.
    path.write_text(DEFAULT_TOML.split("# ── The team")[0].format(name=name, local_model=local_model or ""))
    save_team(root, default_team(local_model), overwrite=False)
    return path


def config_notice(root: Path, key: str, text: str) -> None:
    from .store import Store
    store = Store(root / STATE_DIR / "troupe.db")
    with store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        if not store.kv_get(key):
            store.event("system", "config", text, significant=False)
            store.kv_set(key, True)


def read_toml(root: Path) -> dict:
    try:
        return tomllib.loads((root / STATE_DIR / CONFIG_FILE).read_text())
    except (OSError, ValueError) as e:
        raise ValueError(f"{CONFIG_FILE}: {e}") from e


def read_team(root: Path) -> dict:
    try:
        document = yaml_codec().load((root / STATE_DIR / TEAM_FILE).read_text())
        if not isinstance(document, dict):
            raise ValueError("expected a mapping")
        return document
    except (OSError, ValueError, YAMLError) as e:
        raise ValueError(f"{TEAM_FILE}: {e}") from e


def migrate_team(root: Path, raw: dict) -> None:
    if not (root / STATE_DIR / TEAM_FILE).exists():
        if not raw.get("agents"):
            raise ValueError("team.yaml: agents: no team or legacy [[agents]] found")
        if not isinstance(raw["agents"], list) or not all(isinstance(a, dict) for a in raw["agents"]):
            raise ValueError("troupe.toml: agents: expected [[agents]] tables")
        agents = []
        for legacy in raw["agents"]:
            row = dict(legacy)
            row["provider"] = row.pop("backend", "claude")
            row["level"] = row.pop("effort", "")
            agents.append(row)
        document = {"agents": agents}
        parse_agents(document)
        save_team(root, document, overwrite=False)
        config_notice(root, "config.migrated_team", "Migrated troupe.toml [[agents]] to team.yaml")
    if raw.get("agents"):
        config_notice(root, "config.ignored_agents", "troupe.toml [[agents]] ignored; team.yaml defines the team")


def nonnegative(value: object, field: str, file: str, agent: str = "config") -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{file}: {agent}: {field} must be a non-negative number")


def parse_agents(document: dict) -> list[AgentCfg]:
    rows = document.get("agents")
    if not isinstance(rows, list) or not rows:
        raise ValueError("team.yaml: agents must be a non-empty list")
    reserved = {r.get("id") for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)}
    seen = set()
    agents = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("team.yaml: agent: expected a mapping")
        role = row.get("role")
        aid = row.get("id")
        if aid is None:
            number = 1
            while f"{role}_{number}" in reserved | seen:
                number += 1
            aid = f"{role}_{number}"
        def error(field: str, message: str) -> None:
            raise ValueError(f"team.yaml: {aid}: {field}: {message}")
        if not isinstance(aid, str) or not aid.strip():
            error("id", "must be a non-empty string")
        if aid.casefold() in ("human", "system", "user", "owner", "team", "all", "everyone"):
            error("id", "reserved identity; choose an agent id")
        if aid in seen:
            error("id", "duplicate id")
        seen.add(aid)
        try:
            get_role(role)
        except (KeyError, TypeError):
            error("role", f"unknown role {role!r}")
        entries = row.get("providers") if "providers" in row else [row]
        if not isinstance(entries, list) or not entries:
            error("providers", "must be a non-empty ordered list")
        providers = []
        unique = set()
        for entry in entries:
            if not isinstance(entry, dict):
                error("providers", "entry must be a mapping")
            provider, model, level = entry.get("provider"), entry.get("model", ""), entry.get("level", "")
            if provider not in ("claude", "codex", "local"):
                error("provider", f"unknown provider {provider!r}")
            if not isinstance(model, str):
                error("model", "must be a string")
            if level not in ("", "low", "medium", "high", "max"):
                error("level", f"invalid level {level!r}")
            if (provider, model) in unique:
                error("providers", "duplicate provider/model")
            unique.add((provider, model))
            providers.append(ProviderCfg(provider, model, level))
        if "idle_minutes" in row and row["idle_minutes"] is not None:
            nonnegative(row["idle_minutes"], "idle_minutes", TEAM_FILE, aid)
        if not isinstance(row.get("enabled", True), bool):
            error("enabled", "must be boolean")
        if not isinstance(row.get("name", aid), str):
            error("name", "must be a string")
        args = row.get("extra_args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            error("extra_args", "must be a list of strings")
        first = providers[0]
        agents.append(AgentCfg(aid, role, row.get("name", aid), first.provider, first.model,
                               row.get("enabled", True), row.get("idle_minutes"), first.level, list(args), providers))
    if sum(a.role == "lead" for a in agents) != 1:
        raise ValueError("team.yaml: agents: role: exactly one lead is required")
    from .store import HandleBook
    HandleBook("", agents)  # aliases and full handles must be unambiguous
    return agents


def load(root: Path, *, toml_data: dict | None = None, team_data: dict | None = None) -> Config:
    raw = read_toml(root) if toml_data is None else toml_data
    if team_data is None:
        migrate_team(root, raw)
        team_data = read_team(root)
    agents = parse_agents(team_data)
    for section in ("project", "budget", "backends", "git", "triage", "notify"):
        if not isinstance(raw.get(section, {}), dict):
            raise ValueError(f"troupe.toml: {section}: expected a table")
    for section in ("budget", "backends", "git"):
        for key, value in raw.get(section, {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                nonnegative(value, f"{section}.{key}", CONFIG_FILE)
    for key in Budget.__dataclass_fields__:
        if key in raw.get("budget", {}):
            nonnegative(raw["budget"][key], f"budget.{key}", CONFIG_FILE)
    if "local_max_steps" in raw.get("backends", {}):
        nonnegative(raw["backends"]["local_max_steps"], "backends.local_max_steps", CONFIG_FILE)
        if not isinstance(raw["backends"]["local_max_steps"], int):
            raise ValueError("troupe.toml: backends.local_max_steps must be an integer")
    limits = team_data.get("provider_limits", {})
    if not isinstance(limits, dict):
        raise ValueError("team.yaml: provider_limits: expected a mapping")
    for provider, windows in limits.items():
        if provider not in ("claude", "codex", "local") or not isinstance(windows, dict):
            raise ValueError(f"team.yaml: provider_limits.{provider}: invalid provider/windows")
        for window, cap in windows.items():
            nonnegative(cap, f"provider_limits.{provider}.{window}", TEAM_FILE)
            if cap > 100:
                raise ValueError(f"team.yaml: provider_limits.{provider}.{window}: maximum is 100")
    from .safety import parse_settings
    from .gates import guard_config
    cfg = Config(
        root=root, project=raw.get("project", {}).get("name", root.name), agents=agents,
        budget=Budget(**{k: v for k, v in raw.get("budget", {}).items() if k in Budget.__dataclass_fields__}),
        backends=Backends(**{k: v for k, v in raw.get("backends", {}).items() if k in Backends.__dataclass_fields__}),
        git_autocommit=raw.get("git", {}).get("autocommit", True),
        git=GitSettings(**{k: v for k, v in raw.get("git", {}).items() if k in GitSettings.__dataclass_fields__}),
        notify=NotifySettings(**raw.get("notify", {})),
        provider_limits=limits, team_data=team_data, toml_data=raw,
        triage=TriageSettings(**raw.get("triage", {})),
        safety=parse_settings(raw.get("safety", {}), root),
    )

    guard_config(cfg)
    return cfg


def load_runtime(root: Path) -> Config:
    """Let tool servers use the engine's last good config while edits are invalid."""
    from .store import Store
    snapshot = Store(root / STATE_DIR / "troupe.db").kv_get("config.last_good")
    try:
        if snapshot and not (root / STATE_DIR / TEAM_FILE).exists():
            raise ValueError("team.yaml is missing")
        return load(root)
    except (ValueError, TypeError, KeyError):
        if not snapshot:
            raise
        return load(root, toml_data=snapshot["toml"], team_data=snapshot["team"])
