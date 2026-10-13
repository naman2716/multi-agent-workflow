# Orchestra

A multi-agent coding orchestrator on the **Claude Agent SDK**. Give it a big goal; it plans a
task graph, runs workers in parallel (each in its own git worktree), checks every change with
deterministic gates and an independent reviewer, and lands approved work on an integration
branch for you to PR. Built from the patterns in Karun Japhet's
*Multi-Agent Development Workflows with Claude Code*, adapted to run unattended.

```
goal ─► Planner (Opus, read-only, at the integration head) ─► Critic ─► task graph
        ──(you approve)──►  queue (SQLite)   ready = deps merged, longest chain first
              ┌──────────────────────────────────┤  up to N in parallel, one env slot each
              ▼
   fresh worktree from integration (+ cloned deps, setup_cmd)
   [test-first cards] test writer → failing acceptance tests → locked
   Worker (sandboxed, fenced by hooks) → structured report
   commit → gates: secrets · conflict markers · diff size · scope · your lint/typecheck/tests
        fail → feedback → worker fixes (same session, ≤2 rounds)
   squash to one commit → Reviewer (other model family, can run tests, never edits)
        blocking findings → validators confirm or refute each
        fixable reject → worker revises in the same session → review again
        wrong approach → fresh attempt (≤ max_attempts)
        attempts used up / worker blocked → triage: rewrite · stronger model · split · or you
   [merge lock] rebase on integration → re-gate if base moved → CAS fast-forward
        conflict → worker resolves markers in its session → gates → review → merge
              ▼
   orchestra/integration (one commit per card)
        ──► push + one draft PR into main ──► CI red → fix card → push again (≤ 2 rounds)
        ──► you review and merge
   questions, blocked cards, PR links → your phone (ntfy); answer there with a tap
```

## Why it's built this way

| Failure mode from the research | What Orchestra does |
|---|---|
| Vague cards → scope creep | Planner must emit acceptance criteria, non-goals and `files_scope`; reviewer rejects creep |
| Agents overwrite each other | One worktree per task; overlapping scopes are auto-serialized in the plan |
| Agent "marks its own homework" | Reviewer is a separate session on the other model family; for money/data/auth cards a separate agent writes locked acceptance tests first |
| Reviewer false alarms throw away work | Each blocking finding is verified by a validator; fixable problems are revised in the same session instead of restarting |
| Silent bad handoffs | Every agent returns schema-validated JSON; handoffs saved in `.orchestra/handoffs/` |
| Comprehension debt | Diff-size gate (default 600 lines) |
| Merge races | Serialized merges, rebase + re-gate, compare-and-swap ref update; conflicts resolved by the card's own worker |
| Parallel test runs collide | Each running card gets a slot with its own ports and DB suffix (`[env]`) |
| Runaway cost / time | Per-agent `max_budget_usd` + `max_turns`, a budget per `orchestra run` |
| Dangerous actions | OS sandbox for worker shell commands (writes only in the worktree, network only to package registries and GitHub), plus PreToolUse guards: no git state changes, no secrets written or read, no `rm -rf ..` |
| Reviewer approves its own red flags | An approval with unmet criteria, blocker/major issues or scope creep counts as a rejection |
| Crashes mid-run | State in SQLite; interrupted cards resume their own session (or resume at review); one live run per repo |
| `main` getting hit | Work lands on `orchestra/integration`; your checkout is never touched; PRs are drafts, never force-pushed |
| Blocked cards wait for you | A triage agent first tries a clearer card, a stronger model, or a split (once per card) |
| Finished work sits on a branch | A run that merged work opens/updates a draft PR and turns red CI into a fix card |
| Questions need you at the terminal | ntfy pushes them to your phone with answer buttons |

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

| Profile | planner + critic | reviewer | small worker | large worker | validator, test writer |
|---|---|---|---|---|---|
| `economy` | sonnet | sonnet | haiku | sonnet | sonnet |
| `balanced` *(default)* | opus | sonnet | sonnet | opus | opus |
| `quality` | opus | opus | sonnet | opus | opus |

The planner runs once per goal, so Opus there is cheap leverage. The reviewer runs on every
attempt, so it's the main cost driver; that's why `balanced` uses Sonnet for it. With
`[review].reviewer_rule = "opposite"` (default) a reviewer of the same family as the card's
worker is swapped for the other of Opus/Sonnet, so in `balanced` Sonnet-written cards get an
Opus review. Validators run only when a review would reject; the test writer only on
`test_first` cards.
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
# or from a Linear ticket (you always start plans yourself):
orchestra plan --linear ENG-123 "skip the admin screen for now"
# review the printed task graph; edit .orchestra/plan.json if needed
orchestra approve                   # (or answer y at the prompt / pass --approve)

orchestra run -j 3 -v               # parallel workers; -v streams tool calls
orchestra status                    # queue + spend
orchestra status T03                # history, feedback, why it was blocked
orchestra retry T03 --note "Use the existing FCM helper in src/notify"
orchestra test-first T04            # locked acceptance tests before the worker (or --off)
orchestra ship                      # push + draft PR + CI by hand (runs do it with [ship].auto)
orchestra ntfy setup                # once per project: questions and results on your phone
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
Asking never counts as a failed attempt. `[notify] command` can also ping your desktop, and
with ntfy (below) you can answer from your phone. Use `orchestra run --no-ask` to keep the
terminal quiet.

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

## Keeping good work: revise, resolve, verify

- **Fixable rejections are revised in place.** The reviewer says whether its findings are
  `fixable`. If so, they go back to the same worker session (`[review].fix_rounds`, default 1),
  gates run, and a fresh reviewer checks the revision. Only a wrong approach starts the card over.
- **Merge conflicts are resolved by the worker.** Each card is squashed to one commit after its
  gates pass. If it conflicts with work merged meanwhile, Orchestra merges the latest
  integration into the worktree and the worker resolves the markers in its session
  (`[review].conflict_rounds`, default 1); gates (incl. a conflict-marker check) and a review of
  the resolution follow. A whole-change merge that applies cleanly needs no agent.
- **The reviewer can run code and isn't the author's model.** With `reviewer_rule = "opposite"`
  Sonnet-written cards are reviewed by Opus and Opus-written by Sonnet. It has sandboxed Bash
  (`reviewer_shell`) to run tests; it can't edit, and whatever it leaves is discarded.
- **Rejections are verified.** Each blocking finding (unmet criterion, blocker/major issue,
  scope creep; up to 5) goes to a validator agent (`validator` model). Only findings it refutes
  with evidence are dropped; if none remain, the card is approved. Validators run only on
  rejections.

## Test-first cards

Cards that change money, stored data or auth/permissions get `test_first` from the planner (and
`/groom`); flip it with `orchestra test-first T03` or `--off`. Before the worker starts, a test
writer (`test_writer` model) writes failing acceptance tests, only in files matching
`[quality].test_globs` (anything else it changes, and any deletion, is put back). They are
committed and **locked**: the worker's guard blocks edits, and Orchestra restores the original
versions before every gate run, so the gates always check the tests as written. Locked tests
don't count toward the scope and diff-size gates. Later attempts reuse them. If they already pass
before implementation, the run log and the reviewer get a warning. The reviewer is also asked to
check that the change doesn't stop them from running (skip markers, conftest or test-config
edits); Orchestra doesn't check that itself.

## Self-healing: triage before a card is blocked

When a card uses up its attempts, or its worker (or test writer) reports it can't proceed, a
read-only triage agent (planner model) reads the card, every note on it, its events and the
last work report and review, looks at the code at the integration head, and picks one action:

- **rewrite**: clearer spec or criteria, or a corrected `files_scope`. The card runs again from
  attempt 1; with a new scope it first waits for open cards touching the same files. On a
  test-first card, new criteria (or `regenerate_tests`) mean the locked tests are written again.
- **escalate**: run it again on a stronger model (usually Opus). If it already ran on that
  model family, it goes to you instead.
- **split**: 2–4 smaller cards, each inside the original `files_scope` (anything outside goes to
  you). Cards that waited on the original wait on all of them; the original ends `cancelled`
  with "split into T08, T09" (`retry` refuses it without `--force`).
- **human**: it stays blocked, with the triage note on the card and in the notification.

Each card is triaged at most once, never past the run budget, and you're notified of what it
did (`orchestra status T03` shows it). `[autonomy].triage_actions` limits what is applied
automatically (others become a note for you); `triage = false` turns it off. If a usage limit
stops a triage, it runs at the start of the next run.

## Shipping: draft PR and CI fix cards

With `[ship].auto = true` (set by `orchestra init`; add a `[ship]` section to older configs), a
run that merged at least one card ends by:

1. pushing `orchestra/integration` to `[ship].remote` (plain push: a rejected push is reported,
   never forced; `main` is never pushed);
2. opening one draft PR into the base branch with `gh`, or updating the open one: title from the
   plan's goal, one section per card (summary, decisions, test-first note, attempts, cost) and
   `Part of ENG-123` lines for Linear cards (links them without closing them on merge);
3. waiting up to `ci_wait_minutes` for the checks of the pushed commit;
4. if CI is red: queuing a **Fix CI** card (failing checks and the tail of each failing GitHub
   Actions log in its spec; scope = the files the PR changes plus test files), working it, and
   shipping again, at most `ci_fix_rounds` times (default 2). Cards you answer while CI runs are
   worked and shipped too.

You get the PR link (and CI result) in the run output, `orchestra status` and a notification.
Needs the GitHub CLI logged in (`gh auth login`) on the machine that runs Orchestra; without a
remote or `gh` it says so and the run still succeeds. `orchestra ship` does the same by hand
(`--no-wait` to skip CI).

## Answer from your phone (ntfy)

```bash
orchestra ntfy setup     # two random topic names, kept in .orchestra/ntfy.json (not committed)
orchestra ntfy test      # a sample question; tap a button to check the round trip
```

Install the ntfy app (iOS/Android, or ntfy.sh/app) and subscribe to the printed topic. Each
question then arrives with buttons for the recommended option and one other, plus **Other
answer…**, which opens the reply topic where you type `T03 2`, `T03 r` or `T03: your words`.
The run reads replies every 10 s while a card waits, and the card resumes exactly as if you had
answered in the terminal (you get a ✓ back). Blocked cards, triage actions, PR links, pauses
and run results arrive there too, titled with the project name.

On public ntfy.sh the topic names are the only secret, which is why they are random and stay
out of `orchestra.toml`. A reply can only answer a card that is waiting, and a button only
answers the question it came with. For a protected or self-hosted server set `[ntfy].server`
and an access token in `NTFY_TOKEN`.

## Linear tickets (you start them)

Nothing in Orchestra picks up work by itself. When you want to plan from a ticket:

```bash
export LINEAR_API_KEY=lin_api_...        # Linear → Settings → Security & access → API keys
orchestra linear show ENG-123            # the ticket as plan input (title, description, comments)
orchestra plan --linear ENG-123 "notes"  # plan from it; your notes take priority
```

The cards remember the ticket (`source`), and the PR links back to it. With
`[linear].comment = true`, Orchestra comments on the ticket when its cards are queued and when
the PR opens. In Claude Code, `/groom ENG-123` reads the ticket the same way.

## Per-card environment and faster setup

- Each running card gets a slot (1..concurrency) and `ORCHESTRA_SLOT`, `ORCHESTRA_TASK`,
  `ORCHESTRA_PORT_BASE` (`port_base + slot * port_span`), `ORCHESTRA_DB_SUFFIX` (`_orch<slot>`),
  plus your own `[env.vars]` with `{slot}`, `{port_base}`, `{task}` filled in, e.g.
  `DATABASE_URL = "postgres://localhost/restocare_test_{slot}"`. Setup, gates and every agent
  of that card see them; point your test config at them so parallel cards don't collide.
- `[quality].clone_paths = ["node_modules"]` copies those (gitignored) folders from your
  checkout copy-on-write (APFS clone on macOS); `setup_cmd` is skipped when they all copied and
  the lockfiles match. Avoid cloning a Python `.venv`: its scripts and editable installs point
  back into your checkout, so tests could run your checkout's code instead of the card's.
- A gate can set `changed_cmd = "python -m pytest -q {files}"`: in fix rounds it runs on the
  changed test files first and fails fast; the full `cmd` still has to pass. "Nothing collected"
  exits (pytest 4/5) don't count as failures.
- If the run budget is reached while a card is between rounds (a revision or a conflict to
  resolve), the card is parked with its worktree and session and continues on the next run.

## Planning

`orchestra plan` drafts against the integration branch (in a read-only `_plan` worktree, or the
graph worktree), not your checkout. A critic agent (`[planning].critic`) checks coverage, paths,
ordering, card size, criteria and `test_first`, and its corrected plan replaces the draft when it
is valid; its notes are printed under "Critic". Scope paths whose folder doesn't exist are
flagged. Ready cards that more work waits on are dispatched first.

## Safety: sandbox, guards and the run lease

- **Sandbox (on by default).** Worker shell commands run in Claude Code's OS sandbox (Seatbelt
  on macOS, bubblewrap on Linux). They can write only inside their worktree, temp and package
  caches, reach only the hosts in `[sandbox].allowed_domains`, and can't read `~/.ssh`, `~/.aws`
  and similar. If the sandbox can't start, the worker fails instead of running unsandboxed.
  `setup_cmd` and gates run outside it, as before. A tool that can't work sandboxed goes in
  `excluded_commands`; `enabled = false` turns it off.
- **Guards (second layer).** PreToolUse hooks block git state changes (also after `git -C`/`-c`,
  and `branch -f`, `config`, `stash`), shell redirects outside the worktree, and reading `.env`
  or credential files (`.env.example` is fine). Planner and reviewer get the read block too.
- **No shared memory.** Every agent runs with Claude Code auto memory off, since it is shared
  by all worktrees of a repo.
- **One run at a time.** `orchestra run` takes a lease in `.orchestra/state.db`; a second run
  exits with code 3 and names the holder. A lease with no heartbeat for 60 s (or a dead pid) is
  taken over. `cancel` on a card a live run is working on, and `retry` on a merged or running
  card, need `--force`.
- **Budget.** `total_budget_usd` caps each run; `orchestra status` shows lifetime spend, the
  last run's spend and the cap.

## Tuning

- **Gates are the backbone.** Without real tests, the reviewer is all you have. Put fast checks
  first; set `setup_cmd` (e.g. `npm ci`) so each worktree can run them. The worker's card lists
  every gate command so it runs them before reporting done. Mark cheap gates `fast = true`.
- **Lockfiles don't count** toward `max_diff_lines` (`[quality].diff_exclude`).
- **`gates_on_stop` (opt-in).** A Stop hook runs the `fast` gates when the worker reports done
  and sends it back once with the failures. Enable it after the smoke test below shows it works
  with your SDK version.
- **Concurrency 2–4.** Rate limits are pooled across all sessions on your account.
- **Models:** Opus plans and reviews; Sonnet does `small` tasks, Opus does `large` ones.
- **Cost:** expect roughly 2–5× a single agent. The live smoke test (1 small task:
  worker + gates + review + merge) cost about $0.18 with Haiku/Sonnet.
- Exit code is 2 when tasks end blocked, so it can run in CI or on a schedule; 3 when another
  run already holds the queue.

### Live smoke test (after upgrading Orchestra or the SDK)

The unit tests use a fake agent, so check the real SDK once on a throwaway repo (~$0.20):

```bash
mkdir /tmp/smoke && cd /tmp/smoke && git init -q && echo 'def add(a, b): return a + b' > calc.py
printf 'import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(1, 2), 3)\n' > test_calc.py
git add -A && git commit -qm init && orchestra init
# then edit orchestra.toml: profile = "economy", gates_on_stop = true, and add the gate
#   [[quality.gates]]
#   name = "test"
#   cmd = "python -m unittest -q"
#   fast = true
echo '[{"key":"mul","title":"Add mul()","spec":"Add mul(a,b) to calc.py with a test","acceptance_criteria":["mul(2,3)==6 tested"],"non_goals":[],"files_scope":["calc.py","test_calc.py"],"depends_on":[],"complexity":"small"}]' > card.json
orchestra add card.json && orchestra run -v
```

Expect: the worker's Bash calls succeed inside the sandbox, the card merges, and
`orchestra status T01` shows no `Worker run failed`. If the run fails with a sandbox or
structured-output error, set `gates_on_stop = false` (and, if needed, `[sandbox] enabled = false`)
and tell me the error.

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
  ship.py          push, draft PR upsert and CI checks/logs through gh
  ntfy.py          phone notifications, answer buttons, reply parsing
  linear.py        Linear GraphQL client (read a ticket, comment)
  cli.py           init / plan / approve / add / run / status / retry / cancel /
                   ship / ntfy / linear / graph / stats / harness / hook
tests/             101 tests with scripted fake agents, a fake gh and fake ntfy/Linear
                   (no API calls); graph tests run real graphify and skip without it
openspec/          specs per module and proposed/archived changes
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
- After you **squash-merge** the PR, `orchestra/integration` still holds the original commits,
  so the next PR would show them again. Once no cards are open, reset it with
  `git fetch origin && git branch -f orchestra/integration origin/main`, and delete the merged
  branch on GitHub if it wasn't deleted automatically (`git push origin --delete
  orchestra/integration`), so the next push starts it fresh. Merge commits don't need this.
- CI fix cards get log tails only from GitHub Actions; other checks get their link.
