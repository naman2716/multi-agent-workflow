"""Configuration loaded from orchestra.toml at the repo root."""
from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .graph import GraphSettings


@dataclass
class Gate:
    name: str
    cmd: str
    timeout_s: int = 900
    fast: bool = False  # cheap check (lint, typecheck); used by the optional Stop-hook pre-check


FAST_GATE_NAMES = ("lint", "typecheck", "type-check", "analyze", "vet", "format")

# Changed lines in these files don't count toward max_diff_lines.
DEFAULT_DIFF_EXCLUDE = ["*.lock", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml",
                        "yarn.lock", "pubspec.lock", "go.sum", "poetry.lock", "uv.lock", "Cargo.lock"]

# Worker shell sandbox defaults: package registries and GitHub only.
DEFAULT_ALLOWED_DOMAINS = [
    "registry.npmjs.org", "registry.yarnpkg.com",           # npm / yarn
    "pypi.org", "files.pythonhosted.org",                    # pip
    "pub.dev",                                               # dart / flutter
    "proxy.golang.org", "sum.golang.org",                    # go modules
    "github.com", "api.github.com", "codeload.github.com",
    "objects.githubusercontent.com", "raw.githubusercontent.com",
]
DEFAULT_ALLOW_WRITE = ["~/.npm", "~/.cache", "~/Library/Caches", "~/.pnpm-store", "~/Library/pnpm",
                       "~/.yarn", "~/.pub-cache", "~/go/pkg/mod"]
DEFAULT_DENY_READ = ["~/.ssh", "~/.aws", "~/.config/gcloud", "~/.gnupg", "~/.netrc",
                     "~/.docker/config.json"]


@dataclass
class Sandbox:
    """OS sandbox for worker Bash commands (Seatbelt on macOS, bubblewrap on Linux)."""
    enabled: bool = True
    allowed_domains: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_DOMAINS))
    allow_write: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOW_WRITE))
    deny_read: list[str] = field(default_factory=lambda: list(DEFAULT_DENY_READ))
    excluded_commands: list[str] = field(default_factory=list)  # run outside the sandbox
    # macOS: let sandboxed commands listen on ports and reach localhost services (test
    # servers, a local database). Without it most backend test suites fail inside the sandbox.
    allow_local_binding: bool = True


MODEL_PROFILES = {
    # Opus is the most capable and the most expensive; on a subscription it also uses up
    # usage limits fastest. The reviewer runs once per attempt, so it matters most.
    "economy":  {"planner": "sonnet", "reviewer": "sonnet", "small_worker": "haiku",  "large_worker": "sonnet"},
    "balanced": {"planner": "opus",   "reviewer": "sonnet", "small_worker": "sonnet", "large_worker": "opus"},
    "quality":  {"planner": "opus",   "reviewer": "opus",   "small_worker": "sonnet", "large_worker": "opus"},
}
DEFAULT_PROFILE = "balanced"


@dataclass
class Limits:
    wait_for_reset: bool = True       # pause and resume automatically when a usage limit hits
    stop_new_at: float = 0.9          # stop starting new agents above this utilization (0-1)
    resume_buffer_s: int = 90         # wait this long past the reported reset time
    fallback_wait_min: int = 30       # when the reset time is unknown, retry after this long
    max_wait_hours: float = 12.0      # give up waiting (run exits; re-run later to resume)


@dataclass
class Config:
    repo: Path
    base_branch: str = "main"
    integration_branch: str = "orchestra/integration"
    state_dir: str = ".orchestra"
    worktree_dir: str = ""  # default: <repo>/../<repo-name>.orchestra-worktrees

    concurrency: int = 3
    max_attempts: int = 3
    total_budget_usd: float = 25.0
    task_budget_usd: float = 4.0
    worker_max_turns: int = 80

    model_profile: str = DEFAULT_PROFILE
    planner_model: str = "opus"
    reviewer_model: str = "sonnet"
    small_worker_model: str = "sonnet"
    large_worker_model: str = "opus"
    escalate_on_retry: bool = True  # retry a failed small card on the large-worker model

    max_diff_lines: int = 600
    diff_exclude: list[str] = field(default_factory=lambda: list(DEFAULT_DIFF_EXCLUDE))
    enforce_file_scope: bool = True
    setup_cmd: str = ""  # run once in each new worktree, e.g. "npm ci"
    gates: list[Gate] = field(default_factory=list)
    gates_on_stop: bool = False  # Stop hook runs fast gates before a worker may finish (opt-in)
    sandbox: Sandbox = field(default_factory=Sandbox)
    project_context: str = ""  # extra conventions every agent should know
    graph: GraphSettings = field(default_factory=GraphSettings)
    limits: Limits = field(default_factory=Limits)
    ask_in_terminal: bool = True     # ask decisions live in the terminal during `run`
    decision_wait_minutes: float = 60  # without a terminal, keep running this long for answers
    notify_cmd: str = ""  # shell command run with $ORCHESTRA_TITLE / $ORCHESTRA_MESSAGE

    @property
    def state_path(self) -> Path:
        return self.repo / self.state_dir

    @property
    def worktree_root(self) -> Path:
        if self.worktree_dir:
            p = Path(self.worktree_dir)
            return p if p.is_absolute() else (self.repo / p).resolve()
        return self.repo.parent / f"{self.repo.name}.orchestra-worktrees"

    @classmethod
    def load(cls, repo: Path) -> "Config":
        repo = repo.resolve()
        path = repo / "orchestra.toml"
        data = tomllib.loads(path.read_text()) if path.exists() else {}
        o = data.get("orchestra", {})
        m = data.get("models", {})
        q = data.get("quality", {})
        gates = [Gate(**g) for g in q.get("gates", [])]
        gr = data.get("graph", {})
        lim = data.get("limits", {})
        sb = data.get("sandbox", {})
        profile = m.get("profile", DEFAULT_PROFILE)
        if profile not in MODEL_PROFILES:
            raise ValueError(f"unknown model profile {profile!r}; use one of {list(MODEL_PROFILES)}")
        pm = {**MODEL_PROFILES[profile], **{k: v for k, v in m.items() if k in MODEL_PROFILES[profile]}}
        return cls(
            repo=repo,
            base_branch=o.get("base_branch", "main"),
            integration_branch=o.get("integration_branch", "orchestra/integration"),
            state_dir=o.get("state_dir", ".orchestra"),
            worktree_dir=o.get("worktree_dir", ""),
            concurrency=o.get("concurrency", 3),
            max_attempts=o.get("max_attempts", 3),
            total_budget_usd=o.get("total_budget_usd", 25.0),
            task_budget_usd=o.get("task_budget_usd", 4.0),
            worker_max_turns=o.get("worker_max_turns", 80),
            model_profile=profile,
            planner_model=pm["planner"],
            reviewer_model=pm["reviewer"],
            small_worker_model=pm["small_worker"],
            large_worker_model=pm["large_worker"],
            escalate_on_retry=m.get("escalate_on_retry", True),
            max_diff_lines=q.get("max_diff_lines", 600),
            diff_exclude=q.get("diff_exclude", list(DEFAULT_DIFF_EXCLUDE)),
            enforce_file_scope=q.get("enforce_file_scope", True),
            setup_cmd=q.get("setup_cmd", ""),
            gates=gates,
            gates_on_stop=q.get("gates_on_stop", False),
            sandbox=Sandbox(**{k: v for k, v in sb.items() if k in Sandbox.__dataclass_fields__}),
            project_context=data.get("project", {}).get("context", ""),
            graph=GraphSettings(enabled=gr.get("enabled", False), python=gr.get("python", ""),
                                refresh_timeout_s=gr.get("refresh_timeout_s", 900)),
            limits=Limits(**{k: v for k, v in lim.items() if k in Limits.__dataclass_fields__}),
            notify_cmd=data.get("notify", {}).get("command", ""),
            ask_in_terminal=o.get("ask_in_terminal", True),
            decision_wait_minutes=o.get("decision_wait_minutes", 60),
        )


def detect_gates(repo: Path) -> tuple[str, list[Gate]]:
    """Best-effort guess of setup + gate commands from the repo's tooling."""
    setup, gates = "", []
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        pm = "pnpm" if (repo / "pnpm-lock.yaml").exists() else \
             "yarn" if (repo / "yarn.lock").exists() else "npm"
        setup = {"npm": "npm ci", "yarn": "yarn install --frozen-lockfile",
                 "pnpm": "pnpm install --frozen-lockfile"}[pm]
        for name in ("lint", "typecheck", "type-check", "test"):
            if name in scripts:
                run = f"{pm} run {name}"
                if name == "test":
                    run += " -- --watchAll=false" if pm == "npm" and "jest" in scripts[name] else ""
                gates.append(Gate(name=name, cmd=run))
    if (repo / "pubspec.yaml").exists():
        setup = setup or "flutter pub get"
        gates += [Gate("analyze", "flutter analyze"), Gate("test", "flutter test")]
    if (repo / "pyproject.toml").exists() or (repo / "requirements.txt").exists():
        gates += [Gate("lint", "ruff check ."), Gate("test", "python -m pytest -q")]
    if (repo / "go.mod").exists():
        gates += [Gate("vet", "go vet ./..."), Gate("test", "go test ./...")]
    for g in gates:
        g.fast = g.name in FAST_GATE_NAMES
    return setup, gates


CONFIG_TEMPLATE = '''# Orchestra config — multi-agent coding orchestrator
[orchestra]
base_branch = "{base}"
# Approved work lands here, never directly on {base}. Review it and open a PR.
integration_branch = "orchestra/integration"
concurrency = 3            # parallel workers (rate limits are pooled across sessions)
max_attempts = 3           # per task, before escalating to a human
total_budget_usd = 25.0    # spending cap for each `orchestra run` (lifetime spend is shown in status)
task_budget_usd = 4.0      # per agent call
worker_max_turns = 80
ask_in_terminal = true     # agents' questions pop up in the terminal during `orchestra run`
decision_wait_minutes = 60 # in the background, keep running this long for answers given elsewhere

[models]
# profile sets all four roles at once:
#   economy  = planner sonnet, reviewer sonnet, small haiku,  large sonnet   (cheapest)
#   balanced = planner opus,   reviewer sonnet, small sonnet, large opus     (default)
#   quality  = planner opus,   reviewer opus,   small sonnet, large opus
profile = "balanced"
# Override any single role (alias like opus/sonnet/haiku, or a full model id):
# planner = "opus"
# reviewer = "sonnet"
# small_worker = "sonnet"
# large_worker = "opus"
# A card can also set its own worker model with a "model" field.
escalate_on_retry = true   # a failed small card retries on large_worker

[limits]
# When your Claude usage limit is hit, pause, wait for the reset, then resume agents
# where they left off. Keep the machine awake (macOS: caffeinate -i orchestra run).
wait_for_reset = true
stop_new_at = 0.9          # stop starting new agents above 90% usage
max_wait_hours = 12

[notify]
# Shell command run for decisions, limit pauses, and run results, with
# $ORCHESTRA_TITLE and $ORCHESTRA_MESSAGE set. Examples:
#   macOS:  osascript -e "display notification \"$ORCHESTRA_MESSAGE\" with title \"$ORCHESTRA_TITLE\""
#   phone:  curl -s -d "$ORCHESTRA_MESSAGE" ntfy.sh/your-private-topic
command = ""

[quality]
max_diff_lines = 600       # reject oversized diffs (comprehension debt)
# files whose changed lines don't count toward max_diff_lines (lockfiles, generated code)
diff_exclude = ["*.lock", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "pubspec.lock", "go.sum", "poetry.lock", "uv.lock", "Cargo.lock"]
enforce_file_scope = true  # reject edits outside a task's declared files
setup_cmd = "{setup}"      # runs once in each new worktree
# Opt-in: a Stop hook runs the `fast` gates before a worker may report done, and sends it
# back once with the failures. Turn on after one live smoke run (see README).
gates_on_stop = false
{gates}
[sandbox]
# Worker shell commands run in Claude Code's OS sandbox (Seatbelt on macOS): writes only in
# the worktree, temp and the caches below; network only to these hosts. setup_cmd and gates
# run outside the sandbox. Set enabled = false to turn it off.
enabled = true
allowed_domains = ["registry.npmjs.org", "registry.yarnpkg.com", "pypi.org", "files.pythonhosted.org", "pub.dev", "proxy.golang.org", "sum.golang.org", "github.com", "api.github.com", "codeload.github.com", "objects.githubusercontent.com", "raw.githubusercontent.com"]
allow_write = ["~/.npm", "~/.cache", "~/Library/Caches", "~/.pnpm-store", "~/Library/pnpm", "~/.yarn", "~/.pub-cache", "~/go/pkg/mod"]
deny_read = ["~/.ssh", "~/.aws", "~/.config/gcloud", "~/.gnupg", "~/.netrc", "~/.docker/config.json"]
excluded_commands = []     # e.g. ["flutter"] to run a tool outside the sandbox
# macOS: allow listening on ports and reaching localhost services (test servers, local DB).
# Turn off if your tests need neither; anything on your localhost is reachable while on.
allow_local_binding = true

[graph]
# Graphify code knowledge graph (pip install "graphifyy[mcp]"). Local tree-sitter parsing,
# no LLM tokens. Refreshed after every merge; agents query it via MCP instead of reading files.
enabled = {graph_enabled}
# python = "/path/to/python"   # interpreter that has graphifyy[mcp]; default: orchestra's own

[project]
context = """
Restocare: restaurant flexistaffing platform (India). Restaurants book on-demand staff
(chefs, cleaners, servers) via the customer app; workers accept gigs via the partner app.
Describe your stack, folder layout, and conventions here; every agent reads this.
"""
'''


def render_gates(gates: list[Gate]) -> str:
    if not gates:
        return ('# Add your checks. Fastest first. Exit code 0 = pass.\n'
                '# [[quality.gates]]\n# name = "test"\n# cmd = "npm test"\n')
    return "".join(f'[[quality.gates]]\nname = "{g.name}"\ncmd = "{g.cmd}"\n'
                   + ("fast = true\n" if g.fast else "") + "\n" for g in gates)
