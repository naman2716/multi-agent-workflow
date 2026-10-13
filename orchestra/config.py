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
    # Optional quick variant run first in fix rounds, with {files} = the changed files
    # (shell-quoted), e.g. "python -m pytest -q {files}". The full cmd must still pass.
    changed_cmd: str = ""


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
    "economy":  {"planner": "sonnet", "reviewer": "sonnet", "small_worker": "haiku",  "large_worker": "sonnet",
                 "validator": "sonnet", "test_writer": "sonnet"},
    "balanced": {"planner": "opus",   "reviewer": "sonnet", "small_worker": "sonnet", "large_worker": "opus",
                 "validator": "opus", "test_writer": "opus"},
    "quality":  {"planner": "opus",   "reviewer": "opus",   "small_worker": "sonnet", "large_worker": "opus",
                 "validator": "opus", "test_writer": "opus"},
}
DEFAULT_PROFILE = "balanced"

DEFAULT_TEST_GLOBS = ["test_*.py", "*_test.py", "tests/*", "*/tests/*", "test/*", "*/test/*",
                      "*/__tests__/*", "__tests__/*", "*.test.*", "*.spec.*", "*_test.go", "*_test.dart",
                      "spec/*", "*/spec/*"]


@dataclass
class Review:
    fix_rounds: int = 1          # in-session revisions after a fixable rejection
    conflict_rounds: int = 1     # in-session merge-conflict resolutions
    validate: bool = True        # validator agents check blocking findings before a reject counts
    max_validations: int = 5
    reviewer_rule: str = "opposite"  # "opposite": reviewer model family differs from the worker's
    reviewer_shell: bool = True  # reviewer/validators may run sandboxed shell commands (tests)


@dataclass
class EnvSettings:
    """Per-slot environment for each running card (setup, gates and agents)."""
    port_base: int = 41000
    port_span: int = 100
    vars: dict[str, str] = field(default_factory=dict)  # values may use {slot} {port_base} {task}


@dataclass
class Planning:
    critic: bool = True          # a critic agent reviews every plan and returns a corrected one


@dataclass
class Autonomy:
    """Self-healing: a triage agent looks at a card before it is blocked."""
    triage: bool = True
    # actions applied automatically; any other choice goes to you as a note
    triage_actions: list[str] = field(default_factory=lambda: ["rewrite", "escalate", "split"])


@dataclass
class Ship:
    """End of run: push the integration branch, open/update a draft PR, fix red CI.
    Off unless the project's orchestra.toml turns it on (`orchestra init` does)."""
    auto: bool = False
    remote: str = "origin"
    draft: bool = True
    gh: str = "gh"                   # GitHub CLI (must be logged in: gh auth login)
    ci_wait_minutes: float = 30      # how long to wait for checks after each push
    ci_fix_rounds: int = 2           # red CI becomes a fix card at most this many times per run
    linear_magic_word: str = "Part of"  # "Part of ENG-123" links the PR without closing the ticket


@dataclass
class NtfySettings:
    """Push notifications and answers from your phone via ntfy (https://ntfy.sh)."""
    server: str = "https://ntfy.sh"
    # Both topic names are secrets on public ntfy.sh. `orchestra ntfy setup` keeps them in
    # .orchestra/ntfy.json (not committed); set them here only if orchestra.toml isn't committed.
    topic: str = ""                  # you subscribe to this one in the ntfy app
    reply_topic: str = ""            # answers are posted here
    token_env: str = "NTFY_TOKEN"    # optional access token for a protected server

    @property
    def enabled(self) -> bool:
        return bool(self.topic)


@dataclass
class LinearSettings:
    api_key_env: str = "LINEAR_API_KEY"
    comment: bool = False            # comment on the ticket when cards are queued and the PR opens


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
    validator_model: str = "opus"
    test_writer_model: str = "opus"
    escalate_on_retry: bool = True  # retry a failed small card on the large-worker model

    max_diff_lines: int = 600
    diff_exclude: list[str] = field(default_factory=lambda: list(DEFAULT_DIFF_EXCLUDE))
    enforce_file_scope: bool = True
    setup_cmd: str = ""  # run once in each new worktree, e.g. "npm ci"
    gates: list[Gate] = field(default_factory=list)
    gates_on_stop: bool = False  # Stop hook runs fast gates before a worker may finish (opt-in)
    sandbox: Sandbox = field(default_factory=Sandbox)
    clone_paths: list[str] = field(default_factory=list)  # copy-on-write from your checkout, e.g. node_modules
    skip_setup_if_cloned: bool = True
    test_globs: list[str] = field(default_factory=lambda: list(DEFAULT_TEST_GLOBS))
    review: Review = field(default_factory=Review)
    env: EnvSettings = field(default_factory=EnvSettings)
    planning: Planning = field(default_factory=Planning)
    project_context: str = ""  # extra conventions every agent should know
    graph: GraphSettings = field(default_factory=GraphSettings)
    limits: Limits = field(default_factory=Limits)
    ask_in_terminal: bool = True     # ask decisions live in the terminal during `run`
    decision_wait_minutes: float = 60  # without a terminal, keep running this long for answers
    notify_cmd: str = ""  # shell command run with $ORCHESTRA_TITLE / $ORCHESTRA_MESSAGE
    autonomy: Autonomy = field(default_factory=Autonomy)
    ship: Ship = field(default_factory=Ship)
    ntfy: NtfySettings = field(default_factory=NtfySettings)
    linear: LinearSettings = field(default_factory=LinearSettings)

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
        rv = data.get("review", {})
        ev = data.get("env", {})
        pl = data.get("planning", {})

        def section(cls_, key):
            return cls_(**{k: v for k, v in data.get(key, {}).items() if k in cls_.__dataclass_fields__})
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
            validator_model=pm["validator"],
            test_writer_model=pm["test_writer"],
            escalate_on_retry=m.get("escalate_on_retry", True),
            max_diff_lines=q.get("max_diff_lines", 600),
            diff_exclude=q.get("diff_exclude", list(DEFAULT_DIFF_EXCLUDE)),
            enforce_file_scope=q.get("enforce_file_scope", True),
            setup_cmd=q.get("setup_cmd", ""),
            gates=gates,
            gates_on_stop=q.get("gates_on_stop", False),
            sandbox=Sandbox(**{k: v for k, v in sb.items() if k in Sandbox.__dataclass_fields__}),
            clone_paths=q.get("clone_paths", []),
            skip_setup_if_cloned=q.get("skip_setup_if_cloned", True),
            test_globs=q.get("test_globs", list(DEFAULT_TEST_GLOBS)),
            review=Review(**{k: v for k, v in rv.items() if k in Review.__dataclass_fields__}),
            env=EnvSettings(**{k: v for k, v in ev.items() if k in EnvSettings.__dataclass_fields__}),
            planning=Planning(**{k: v for k, v in pl.items() if k in Planning.__dataclass_fields__}),
            project_context=data.get("project", {}).get("context", ""),
            graph=GraphSettings(enabled=gr.get("enabled", False), python=gr.get("python", ""),
                                refresh_timeout_s=gr.get("refresh_timeout_s", 900)),
            limits=Limits(**{k: v for k, v in lim.items() if k in Limits.__dataclass_fields__}),
            notify_cmd=data.get("notify", {}).get("command", ""),
            ask_in_terminal=o.get("ask_in_terminal", True),
            decision_wait_minutes=o.get("decision_wait_minutes", 60),
            autonomy=section(Autonomy, "autonomy"),
            ship=section(Ship, "ship"),
            ntfy=_with_saved_topics(section(NtfySettings, "ntfy"),
                                    repo / o.get("state_dir", ".orchestra") / NTFY_TOPICS_FILE),
            linear=section(LinearSettings, "linear"),
        )


NTFY_TOPICS_FILE = "ntfy.json"


def _with_saved_topics(s: NtfySettings, path: Path) -> NtfySettings:
    """Fill empty topic names from the file `orchestra ntfy setup` writes (gitignored)."""
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        return s
    s.topic = s.topic or str(saved.get("topic") or "")
    s.reply_topic = s.reply_topic or str(saved.get("reply_topic") or "")
    return s


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
        gates += [Gate("lint", "ruff check ."),
                  Gate("test", "python -m pytest -q", changed_cmd="python -m pytest -q {files}")]
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
# profile sets all roles at once:
#   economy  = planner sonnet, reviewer sonnet, small haiku,  large sonnet, validator/test writer sonnet
#   balanced = planner opus,   reviewer sonnet, small sonnet, large opus,   validator/test writer opus
#   quality  = planner opus,   reviewer opus,   small sonnet, large opus,   validator/test writer opus
# With [review].reviewer_rule = "opposite", a reviewer of the same family as the card's worker is
# swapped for the other of opus/sonnet (balanced: Sonnet work -> Opus review, Opus work -> Sonnet).
profile = "balanced"
# Override any single role (alias like opus/sonnet/haiku, or a full model id):
# planner = "opus"
# reviewer = "sonnet"
# small_worker = "sonnet"
# large_worker = "opus"
# validator = "opus"
# test_writer = "opus"
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
# $ORCHESTRA_TITLE and $ORCHESTRA_MESSAGE set. Example (macOS):
#   osascript -e "display notification \"$ORCHESTRA_MESSAGE\" with title \"$ORCHESTRA_TITLE\""
# For your phone (with answer buttons), use [ntfy] below instead.
command = ""

[ntfy]
# Questions arrive on your phone with answer buttons; blocked cards, PRs and run results too.
# Run `orchestra ntfy setup`: it makes two random topic names and keeps them in
# .orchestra/ntfy.json (not committed). Anyone who knows a topic name can read or post to it
# on public ntfy.sh, so don't put them in this file if it is committed.
server = "https://ntfy.sh"
token_env = "NTFY_TOKEN"   # optional access token (protected or self-hosted server)

[autonomy]
# Before a card is blocked, a triage agent may rewrite it, retry it on a stronger model, or
# split it into smaller cards inside its original files_scope. Once per card; you're notified.
triage = true
triage_actions = ["rewrite", "escalate", "split"]

[ship]
# After a run that merged work: push the integration branch, open or update one draft PR into the base
# branch (GitHub CLI: gh auth login), wait for CI, and turn red checks into a fix card.
auto = true
remote = "origin"
draft = true
ci_wait_minutes = 30
ci_fix_rounds = 2
linear_magic_word = "Part of"   # links Linear tickets without closing them on merge

[linear]
# Only for `orchestra plan --linear ENG-123`: nothing is ever started from Linear on its own.
api_key_env = "LINEAR_API_KEY"  # personal API key, read from this environment variable
comment = false                 # comment on the ticket when cards are queued and the PR opens

[quality]
max_diff_lines = 600       # reject oversized diffs (comprehension debt)
# files whose changed lines don't count toward max_diff_lines (lockfiles, generated code)
diff_exclude = ["*.lock", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "pubspec.lock", "go.sum", "poetry.lock", "uv.lock", "Cargo.lock"]
enforce_file_scope = true  # reject edits outside a task's declared files
setup_cmd = "{setup}"      # runs once in each new worktree
# copy these from your checkout into each worktree (copy-on-write on macOS APFS), e.g.
# ["node_modules"]; setup_cmd is skipped when they copied and the lockfiles match
clone_paths = []
skip_setup_if_cloned = true
# files a test-first card's test writer may create (globs on the path or file name)
# test_globs = ["test_*.py", "*_test.py", "tests/*", "*.test.*", "*.spec.*", "*_test.go", "*_test.dart"]
# Opt-in: a Stop hook runs the `fast` gates before a worker may report done, and sends it
# back once with the failures. Turn on after one live smoke run (see README).
gates_on_stop = false
{gates}
[review]
fix_rounds = 1             # a fixable rejection goes back to the same worker session this many times
conflict_rounds = 1        # merge conflicts with newly merged work are resolved by the worker
validate = true            # a validator agent checks each blocking finding before a rejection counts
reviewer_rule = "opposite" # or "profile": always use [models].reviewer
reviewer_shell = true      # reviewer may run tests (sandboxed, no edits)

[env]
# Each running card gets a slot (1..concurrency) and these variables, for setup, gates and agents:
# ORCHESTRA_SLOT, ORCHESTRA_TASK, ORCHESTRA_PORT_BASE (port_base + slot * port_span),
# ORCHESTRA_DB_SUFFIX (_orch<slot>). Point your test config at them to avoid port/DB clashes.
port_base = 41000
port_span = 100
# [env.vars]
# DATABASE_URL = "postgres://localhost/restocare_test_{{slot}}"

[planning]
critic = true              # a critic agent checks each plan and returns a corrected one

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
                   + ("fast = true\n" if g.fast else "")
                   + (f'changed_cmd = "{g.changed_cmd}"\n' if g.changed_cmd else "") + "\n" for g in gates)
