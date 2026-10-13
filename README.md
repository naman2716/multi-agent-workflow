# Orchestra

A multi-agent coding orchestrator on the **Claude Agent SDK**. Give it a big goal; it plans a
task graph, runs workers in parallel (each in its own git worktree), checks every change with
deterministic gates and an independent reviewer, and lands approved work on an integration
branch for you to PR. Built from the patterns in Karun Japhet's
*Multi-Agent Development Workflows with Claude Code*, adapted to run unattended.

```
goal ─► Planner (Opus, read-only) ─► task graph  ──(you approve)──►  queue (SQLite)
                                                                      │  ready = deps merged
              ┌───────────────────────────────────────────────────────┤  up to N in parallel
              ▼                                                       ▼
   fresh worktree from integration                          fresh worktree …
   Worker (Sonnet/Opus, fenced by hooks) → structured report
   commit → gates: secrets · diff size · file scope · your lint/typecheck/tests
        fail → feedback → worker fixes (same attempt, ≤2 rounds)
   Reviewer (separate context, read-only) → criteria · scope creep · issues
        reject → feedback → fresh attempt (≤ max_attempts) → else BLOCKED for a human
   [merge lock] rebase on integration → re-gate if base moved → CAS fast-forward
              ▼
   orchestra/integration  ──► you review, open PR into main
```

## Why it's built this way

| Failure mode from the research | What Orchestra does |
|---|---|
| Vague cards → scope creep | Planner must emit acceptance criteria, non-goals and `files_scope`; reviewer rejects creep |
| Agents overwrite each other | One worktree per task; overlapping scopes are auto-serialized in the plan |
| Agent "marks its own homework" | Reviewer is a separate session that only reads |
| Silent bad handoffs | Every agent returns schema-validated JSON; handoffs saved in `.orchestra/handoffs/` |
| Comprehension debt | Diff-size gate (default 600 lines) |
| Merge races | Serialized merges, rebase + re-gate, compare-and-swap ref update |
| Runaway cost / time | Per-agent `max_budget_usd` + `max_turns`, global run budget |
| Dangerous actions | PreToolUse hooks: no writes outside worktree, no git state changes, no secrets, no `rm -rf ..` |
| Crashes mid-run | State in SQLite; `run` resets interrupted tasks and resumes |
| `main` getting hit | Work lands on `orchestra/integration`; your checkout is never touched |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e /path/to/orchestra          # pulls claude-agent-sdk
pip install "graphifyy[mcp]"               # optional: code knowledge graph (recommended)
```

**Auth.** Either works:
- **Claude Code login** (subscription): run `claude` once and `/login`. Make sure
  `ANTHROPIC_API_KEY` is *not* set, or it takes precedence. Usage counts against your plan's
  limits, which are shared by all your sessions, so keep `-j` at 2–3. The dollar figures in
  `status`/`stats` are then API-equivalent estimates (useful for comparing), not charges.
- **API key**: `export ANTHROPIC_API_KEY=...`, billed per token.

## Choosing models

Pick a **profile**, then override single roles if you like (`orchestra.toml` → `[models]`):

| Profile | planner | reviewer | small worker | large worker |
|---|---|---|---|---|
| `economy` | sonnet | sonnet | haiku | sonnet |
| `balanced` *(default)* | opus | sonnet | sonnet | opus |
| `quality` | opus | opus | sonnet | opus |

The planner runs once per goal, so Opus there is cheap leverage. The reviewer runs on every
attempt, so it's the main cost driver; that's why `balanced` uses Sonnet for it.
`orchestra models` prints the effective setup. Values can be aliases (`opus`, `sonnet`,
`haiku`) or full model ids. A card's worker model is chosen, in order, from **its own
`model` field → escalation after a failed attempt (`escalate_on_retry`) → its complexity.**

Per-card control:
- in a plan or card file: `"model": "opus"` on that card (`/groom` can set it with you);
- on a queued card: `orchestra model T04 opus` (or `default` to clear);
- when retrying: `orchestra retry T04 --model opus --note "..."`.
`orchestra status T04` shows the model a card will run on. Workers' Claude Code harness
agents use the same defaults (frontmatter `model:`), written at `harness install`.

## Use it on Restocare

```bash
cd ~/code/restocare-backend         # any repo: backend, customer app, partner app
orchestra init                      # writes orchestra.toml, detects npm/flutter/pytest/go checks
# edit orchestra.toml: [project].context (stack, folders, conventions), gates, setup_cmd
# a CLAUDE.md in the repo is also loaded by every agent

orchestra plan "Add shift cancellation with a 2-hour penalty window: API endpoint, \
pricing rule, push notification to the partner, and tests"
# review the printed task graph; edit .orchestra/plan.json if needed
orchestra approve                   # (or answer y at the prompt / pass --approve)

orchestra run -j 3 -v               # parallel workers; -v streams tool calls
orchestra status                    # queue + spend
orchestra status T03                # history, feedback, why it was blocked
orchestra retry T03 --note "Use the existing FCM helper in src/notify"
git log main..orchestra/integration # then open a PR
```

### Interactive side: the Claude Code harness

```bash
orchestra harness install    # once per repo; safe to re-run
```

Then, inside Claude Code in that repo:

| Command | What it does |
|---|---|
| `/groom` | Plan a feature *with you*, write tight cards, queue them (`orchestra approve`) |
| `/dispatch` | Run the queue in the background with parallel agents, report merged/blocked |
| `/orchestra-status` | Queue, spend, why cards are blocked, cost with vs without graph |
| `/decide` | Answer the questions agents stopped to ask; they resume where they left off |
| `/review-integration` | Human-level review of everything on `orchestra/integration`, then open a PR |

It also adds `orchestra-planner`, `orchestra-worker` (worktree-isolated) and `orchestra-reviewer`
subagents built from **the same prompts** the unattended agents use, a guard hook
(`orchestra hook interactive`: blocks writes outside the repo, secrets in files, pushes to
main, force-pushes), an Orchestra section in CLAUDE.md, and, with Graphify, its graph-first
hooks, its git hooks (graph rebuilt on every commit/checkout of your checkout) and an MCP
server in `.mcp.json`. Existing settings and CLAUDE.md content are merged, never overwritten.

### Code knowledge graph (Graphify)

With `[graph] enabled = true` (set automatically by `orchestra init` when Graphify is
installed):

- A dedicated worktree tracks `orchestra/integration`; after **every merge** the orchestrator
  runs `graphify update` (tree-sitter only: local, no LLM tokens, about a second on small
  repos), so the graph always matches the latest merged code.
- Each agent gets a **frozen snapshot** matching the code it works on, served over MCP
  (`query_graph`, `get_node`, `get_neighbors`, `shortest_path`, …), and is told to query it
  before grepping or reading whole files.
- **Planning:** tasks that touch different files but call into each other get a dependency
  edge automatically, so coupled work is serialized while unrelated work stays parallel.
- **Review:** the reviewer gets an impact list (code outside the diff that calls changed
  symbols) and checks those still work.
- If Graphify fails, the run continues without it.

```bash
orchestra graph build            # build/refresh now
orchestra graph query "how is a booking priced"
orchestra graph affected "fare"  # what depends on this symbol
orchestra stats                  # avg cost + tokens per card, with vs without graph
```

On savings: Graphify's own benchmark on this repo estimates about 12× fewer tokens per
question, but that is measured against reading the whole codebase. Real agents already read
selectively, so expect less, mostly on large repos and for the planner and reviewer. Measure
it: run comparable cards with `enabled = true` and `false`, then `orchestra stats`.

You can also skip the planner and queue hand-written cards: `orchestra add cards.json`
(same fields as a plan task: key, title, spec, acceptance_criteria, non_goals, files_scope,
depends_on, complexity).

## Agents ask you when it matters

A worker that hits a choice the card doesn't settle and that affects users, money or data
(a pricing rule, a cancellation window, a migration) stops early and asks instead of
guessing. Minor choices it makes itself and notes in its summary.

**In the terminal, the question just shows up.** While `orchestra run` is going, a question
appears with a bell, and other agents keep working (their log lines are held back so the
question stays readable):

```
────────────────────────────────────────────────────────────────
  ❓ T01 needs your decision  ·  Add a late-cancellation fee
────────────────────────────────────────────────────────────────
  What threshold and fee percentage should cancellation_fee use?
  Why it matters: it decides what restaurants are charged …
   1. 24h threshold, 50% fee
   2. 12h threshold, 100% fee
   3. Tiered: 0% if >=24h, 50% if 2-24h, 100% if <2h
  Agent recommends: 1 …
  Type a number, 'r' for the recommendation, your own answer, or 'later'.
  answer › 3
  ✓ T01 resumes with: Tiered: …
```

The agent resumes its own session right away, in the same run. `2: but round up` picks an
option with a tweak (your note wins where they differ). `later` leaves it open.

**Elsewhere, it's picked up live.** When the run has no terminal (e.g. started by `/dispatch`
in Claude Code), it keeps going for up to `decision_wait_minutes` (default 60) after
everything else is done. Answer from Claude Code (`/dispatch` relays questions as they
appear; `/decide` lists them) or from any terminal (`orchestra decisions`,
`orchestra answer T01 --pick 3`), and the agent resumes within seconds, no restart.
Asking never counts as a failed attempt. `[notify] command` can also ping your desktop or
phone. Use `orchestra run --no-ask` to keep the terminal quiet.

## Usage limits: pause and auto-resume

Every agent session reports your usage state. Orchestra watches it:

- above `stop_new_at` (default 90%) it drops to one agent at a time;
- when the limit is hit, it stops starting work, waits until the reported reset time
  (plus a small buffer), then continues. Agents that were cut off **resume their own
  session** with their edits intact, and a pause never uses up an attempt;
- `max_wait_hours` caps the wait; `orchestra run --no-wait` exits instead, and the next
  `orchestra run` resumes everything.

The process must stay alive while it waits. Keep the machine awake
(macOS: `caffeinate -i orchestra run`), or use `--no-wait` and re-run after the reset.
If it's interrupted anyway, re-running picks up where it left off.

## Tuning

- **Gates are the backbone.** Without real tests, the reviewer is all you have. Put fast checks
  first; set `setup_cmd` (e.g. `npm ci`) so each worktree can run them.
- **Concurrency 2–4.** Rate limits are pooled across all sessions on your account.
- **Models:** Opus plans and reviews; Sonnet does `small` tasks, Opus does `large` ones.
- **Cost:** expect roughly 2–5× a single agent. The live smoke test (1 small task:
  worker + gates + review + merge) cost about $0.18 with Haiku/Sonnet.
- Exit code is 2 when tasks end blocked, so it can run in CI or on a schedule.

## Layout

```
orchestra/
  agents.py        planner / worker / reviewer prompts, JSON schemas, ClaudeRunner
  orchestrator.py  the loop: dispatch, fix rounds, review, retries, integration
  planning.py      plan validation, cycle check, auto-serialize overlapping scopes
  gates.py         secrets, diff size, file scope, project commands
  guards.py        SDK PreToolUse hooks fencing workers into their worktree
  gitops.py        worktrees, rebase, CAS fast-forward
  store.py         SQLite task store with atomic claims and event log
  graph.py         Graphify: graph worktree, refresh, snapshots, coupling + impact analysis
  harness.py       Claude Code harness generator (agents, skills, hooks, MCP, CLAUDE.md)
  limits.py        usage-limit tracker (pause/resume) and notifications
  cli.py           init / plan / approve / add / run / status / retry / cancel /
                   graph / stats / harness / hook
tests/             23 end-to-end tests with scripted fake agents (no API calls);
                   graph tests run real graphify
```

Run the tests: `python -m unittest discover -s tests -t . -v`

## Known limits

- Coordination is through the task store, not live agent-to-agent chat (Agent Teams only
  run in interactive Claude Code sessions, not the SDK).
- Cross-repo goals (backend plus both apps) need one run per repo for now.
- The interactive guard blocks writes outside the repo, including `/tmp`; edit `guards.py`
  if you want to allow a scratch directory.
- `.mcp.json` stores this machine's Python path for the graph server; teammates may need to
  edit it (or point it at `uv tool run --from 'graphifyy[mcp]' python`).
- The reviewer catches a lot, but business logic still deserves your read before merging
  to `main`.
