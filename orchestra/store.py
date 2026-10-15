"""SQLite-backed task store.

The store is the single source of truth for the run. Agents never share a
conversation; they share *this* (plus structured handoff files). That keeps
every handoff explicit and lets a crashed run resume.

Task lifecycle:
  pending -> running -> review -> merged
                 |         |
                 +---------+--> pending (retry with feedback)
                 +--> waiting (agent asked the human a question; resumes after `answer`)
                 +--> pending + resume_note (paused by a usage limit; resumes its session)
                 +--> blocked (attempts exhausted / agent says stuck; needs a human)
  Before blocking, a triage agent may rewrite the card (-> pending), move it to a stronger
  model (-> pending), or split it (-> cancelled "split into ...", new cards pending). Once.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    spec          TEXT NOT NULL,
    acceptance    TEXT NOT NULL DEFAULT '[]',
    non_goals     TEXT NOT NULL DEFAULT '[]',
    files_scope   TEXT NOT NULL DEFAULT '[]',
    depends_on    TEXT NOT NULL DEFAULT '[]',
    complexity    TEXT NOT NULL DEFAULT 'small',
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    feedback      TEXT NOT NULL DEFAULT '[]',
    branch        TEXT,
    worktree      TEXT,
    cost_usd      REAL NOT NULL DEFAULT 0,
    summary       TEXT,
    claimed_at    REAL,
    updated_at    REAL NOT NULL,
    created_at    REAL NOT NULL,
    seq           INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   TEXT,
    kind      TEXT NOT NULL,
    detail    TEXT,
    at        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    task_id       TEXT,
    cost_usd      REAL NOT NULL DEFAULT 0,     -- the session's running total as last reported
    tokens        INTEGER NOT NULL DEFAULT 0
);
"""
SESSION_COLUMNS = [  # one row per agent session; a resumed session updates its row
    ("role", "TEXT"),            # worker, test_writer, reviewer, validator, triage
    ("model", "TEXT"),
    ("attempt", "INTEGER"),      # the card's attempt number when the session last ran
    ("started_at", "REAL"),
    ("ended_at", "REAL"),        # end of its latest run
    ("end_reason", "TEXT"),      # done, gate_failed, review_rejected, blocked, needs_decision,
                                 # usage_limit, interrupted, budget, error
    ("runs", "INTEGER NOT NULL DEFAULT 0"),  # first run plus every resume
]
END_REASONS = ("done", "gate_failed", "review_rejected", "blocked", "needs_decision", "usage_limit",
               "interrupted", "budget", "error")
MIGRATIONS = [  # columns added after v0.1; applied idempotently
    ("tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("used_graph", "INTEGER NOT NULL DEFAULT 0"),
    ("model", "TEXT"),
    ("session_id", "TEXT"),      # worker session, resumable after a decision or a limit pause
    ("base_commit", "TEXT"),     # integration head the worktree was cut from
    ("resume_note", "TEXT"),     # message to send when resuming that session
    ("decision", "TEXT"),        # JSON: the open (or answered) question for the human
    ("decisions", "TEXT NOT NULL DEFAULT '[]'"),  # JSON: every answered question, kept on the card
    ("test_first", "INTEGER NOT NULL DEFAULT 0"),  # acceptance tests written first by another agent
    ("locked_tests", "TEXT NOT NULL DEFAULT '[]'"),  # JSON: those test files, locked for the worker
    ("triaged", "INTEGER NOT NULL DEFAULT 0"),  # a triage agent already looked at it (once per card)
    ("source", "TEXT"),          # where the card came from: "linear:ENG-123", "ci", ...
    ("estimated_lines", "INTEGER NOT NULL DEFAULT 0"),  # the plan's size estimate (0 = none)
]

JSON_FIELDS = ("acceptance", "non_goals", "files_scope", "depends_on", "feedback", "decisions",
               "locked_tests")
STATUSES = ("pending", "running", "review", "waiting", "merged", "blocked", "cancelled")
OPEN_STATUSES = ("pending", "running", "review", "waiting")
LEASE_TTL_S = 60  # a run lease with no heartbeat for this long is stale


class RunLocked(RuntimeError):
    """Another live `orchestra run` holds this repository's queue."""

    def __init__(self, holder: dict):
        self.holder = holder
        super().__init__(f"another orchestra run is active (pid {holder.get('pid')} on "
                         f"{holder.get('host')}, started {time.strftime('%H:%M', time.localtime(holder.get('started', 0)))})")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lease_is_live(lease: dict | None, now: float | None = None, ttl: float = LEASE_TTL_S) -> bool:
    if not lease:
        return False
    now = time.time() if now is None else now
    if now - float(lease.get("heartbeat", 0)) > ttl:
        return False
    if lease.get("host") == socket.gethostname() and not _pid_alive(int(lease.get("pid", 0))):
        return False
    return True


@dataclass
class Task:
    id: str
    title: str
    spec: str
    acceptance: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    files_scope: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    complexity: str = "small"
    model: str | None = None  # per-card worker model override (alias or full model id)
    session_id: str | None = None
    base_commit: str | None = None
    resume_note: str | None = None
    decision: dict | None = None
    decisions: list[dict] = field(default_factory=list)  # answered questions: question, answer, at
    test_first: bool = False
    locked_tests: list[str] = field(default_factory=list)
    triaged: bool = False
    source: str | None = None
    estimated_lines: int = 0
    status: str = "pending"
    attempts: int = 0
    feedback: list[str] = field(default_factory=list)
    branch: str | None = None
    worktree: str | None = None
    cost_usd: float = 0.0
    tokens: int = 0
    used_graph: int = 0
    summary: str | None = None
    claimed_at: float | None = None
    updated_at: float = 0.0
    created_at: float = 0.0
    seq: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        d = dict(row)
        for f in JSON_FIELDS:
            d[f] = json.loads(d[f] or "[]")
        d["decision"] = json.loads(d["decision"]) if d.get("decision") else None
        d["test_first"] = bool(d.get("test_first"))
        d["triaged"] = bool(d.get("triaged"))
        return cls(**d)


class Store:
    """Thread-safe store. All state transitions are single SQL statements so
    claims are atomic even if you later run several orchestrator processes."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        for name, ddl in MIGRATIONS:
            if name not in cols:
                self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(sessions)")}
        for name, ddl in SESSION_COLUMNS:
            if name not in cols:
                self.conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {ddl}")

    # ---------- writes ----------
    def add_task(self, task: Task) -> None:
        now = time.time()
        with self._lock:
            seq = self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM tasks").fetchone()[0]
            self.conn.execute(
                """INSERT INTO tasks (id,title,spec,acceptance,non_goals,files_scope,depends_on,
                   complexity,model,status,attempts,feedback,created_at,updated_at,seq,test_first,
                   triaged,source,estimated_lines)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0,'[]',?,?,?,?,?,?,?)""",
                (task.id, task.title, task.spec, json.dumps(task.acceptance),
                 json.dumps(task.non_goals), json.dumps(task.files_scope),
                 json.dumps(task.depends_on), task.complexity, task.model, task.status,
                 now, now, seq, 1 if task.test_first else 0, 1 if task.triaged else 0, task.source,
                 int(task.estimated_lines or 0)),
            )
        self.log(task.id, "created", task.title)

    def ready_ids(self) -> list[str]:
        """Pending tasks whose dependencies are merged (read-only; does not claim)."""
        merged = {r[0] for r in self.conn.execute("SELECT id FROM tasks WHERE status='merged'")}
        return [t.id for t in self.all() if t.status == "pending" and all(d in merged for d in t.depends_on)]

    def _chain_weight(self, tasks: list[Task]) -> dict[str, int]:
        """For each open card: how many open cards depend on it, directly or transitively."""
        open_ = {t.id: t for t in tasks if t.status in OPEN_STATUSES}
        dependents: dict[str, set[str]] = {i: set() for i in open_}
        for t in open_.values():
            for d in t.depends_on:
                if d in dependents:
                    dependents[d].add(t.id)

        def reach(i: str, seen: set[str]) -> set[str]:
            for j in dependents.get(i, ()):
                if j not in seen:
                    seen.add(j)
                    reach(j, seen)
            return seen
        return {i: len(reach(i, set())) for i in open_}

    def claim_ready(self, limit: int) -> list[Task]:
        """Atomically claim up to `limit` pending tasks whose dependencies are all merged,
        cards that more work waits on first (longest chain first), then queue order."""
        claimed: list[Task] = []
        with self._lock:
            tasks = self.all()
            weight = self._chain_weight(tasks)
            merged = {t.id for t in tasks if t.status == "merged"}
            pending = sorted((t for t in tasks if t.status == "pending"),
                             key=lambda t: (-weight.get(t.id, 0), t.seq))
            for t in pending:
                if len(claimed) >= limit:
                    break
                if not all(d in merged for d in t.depends_on):
                    continue
                now = time.time()
                cur = self.conn.execute(
                    "UPDATE tasks SET status='running', claimed_at=?, updated_at=?, attempts=attempts+1 "
                    "WHERE id=? AND status='pending'", (now, now, t.id))
                if cur.rowcount == 1:
                    claimed.append(self.get(t.id))
                    self.log(t.id, "claimed", f"attempt {t.attempts + 1}")
        return claimed

    def update(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        for f in JSON_FIELDS:
            if f in fields:
                fields[f] = json.dumps(fields[f])
        if "decision" in fields:
            fields["decision"] = json.dumps(fields["decision"]) if fields["decision"] is not None else None
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))

    def add_feedback(self, task_id: str, note: str) -> None:
        t = self.get(task_id)
        self.update(task_id, feedback=t.feedback + [note])

    def add_cost(self, task_id: str, usd: float, tokens: int = 0, session_id: str | None = None, *,
                 role: str | None = None, model: str | None = None, attempt: int | None = None,
                 started_at: float | None = None, end_reason: str | None = None) -> None:
        """Add one agent call's spend to the card and record the session's run. A resumed session
        reports its running total (everything since the session began), so for a known session only
        the part since its last report is added; a run cut off before reporting is picked up by the next."""
        usd, tokens = usd or 0.0, tokens or 0
        with self._lock:
            if session_id:
                row = self.conn.execute("SELECT cost_usd, tokens FROM sessions WHERE session_id=?",
                                        (session_id,)).fetchone()
                seen_usd, seen_tok = (row[0], row[1]) if row else (0.0, 0)
                now = time.time()
                self.conn.execute(
                    "INSERT INTO sessions (session_id, task_id, cost_usd, tokens, role, model, attempt, "
                    "started_at, ended_at, end_reason, runs) VALUES (?,?,?,?,?,?,?,?,?,?,1) "
                    "ON CONFLICT(session_id) DO UPDATE SET task_id=excluded.task_id, "
                    "cost_usd=excluded.cost_usd, tokens=excluded.tokens, role=COALESCE(excluded.role, role), "
                    "model=COALESCE(excluded.model, model), attempt=COALESCE(excluded.attempt, attempt), "
                    "ended_at=excluded.ended_at, end_reason=COALESCE(excluded.end_reason, end_reason), "
                    "runs=runs+1",
                    (session_id, task_id, max(usd, seen_usd), max(tokens, seen_tok), role, model, attempt,
                     started_at or now, now, end_reason))
                usd, tokens = max(usd - seen_usd, 0.0), max(tokens - seen_tok, 0)
            self.conn.execute("UPDATE tasks SET cost_usd=cost_usd+?, tokens=tokens+? WHERE id=?",
                              (usd, tokens, task_id))

    def end_session(self, session_id: str | None, reason: str) -> None:
        """Why a session stopped being used, when that is decided after its last run."""
        if session_id:
            with self._lock:
                self.conn.execute("UPDATE sessions SET end_reason=? WHERE session_id=?", (reason, session_id))

    def sessions(self, task_id: str | None = None) -> list[dict]:
        q, args = ("SELECT * FROM sessions WHERE task_id=? ORDER BY started_at", (task_id,)) if task_id \
            else ("SELECT * FROM sessions ORDER BY started_at", ())
        return [dict(r) for r in self.conn.execute(q, args)]

    def interrupted(self) -> list[Task]:
        """Tasks a previous run left 'running'/'review' (it crashed or was stopped)."""
        return [t for t in self.all() if t.status in ("running", "review")]

    # ---------- run lease: one live `orchestra run` per repository ----------
    def acquire_lease(self, ttl: float = LEASE_TTL_S) -> str:
        """Take the run lease or raise RunLocked. Atomic across processes."""
        now = time.time()
        me = {"token": uuid.uuid4().hex, "pid": os.getpid(), "host": socket.gethostname(),
              "started": now, "heartbeat": now}
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute("SELECT value FROM meta WHERE key='run_lease'").fetchone()
                current = json.loads(row[0]) if row else None
                if lease_is_live(current, now, ttl):
                    raise RunLocked(current)
                self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('run_lease', ?)", (json.dumps(me),))
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return me["token"]

    def heartbeat(self, token: str) -> bool:
        """Refresh our lease; False if it was taken over (or released)."""
        with self._lock:
            lease = self.get_meta("run_lease")
            if not lease or lease.get("token") != token:
                return False
            lease["heartbeat"] = time.time()
            self.set_meta("run_lease", lease)
            return True

    def release_lease(self, token: str) -> None:
        with self._lock:
            lease = self.get_meta("run_lease")
            if lease and lease.get("token") == token:
                self.conn.execute("DELETE FROM meta WHERE key='run_lease'")

    def live_lease(self) -> dict | None:
        lease = self.get_meta("run_lease")
        return lease if lease_is_live(lease) else None

    def log(self, task_id: str | None, kind: str, detail: str = "") -> None:
        with self._lock:
            self.conn.execute("INSERT INTO events (task_id,kind,detail,at) VALUES (?,?,?,?)",
                              (task_id, kind, detail[:4000], time.time()))

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # ---------- reads ----------
    def get(self, task_id: str) -> Task:
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return Task.from_row(row)

    def all(self) -> list[Task]:
        return [Task.from_row(r) for r in self.conn.execute("SELECT * FROM tasks ORDER BY seq")]

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for s, n in self.conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status"):
            out[s] = n
        return out

    def total_cost(self) -> float:
        return float(self.conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM tasks").fetchone()[0]) + \
            float(self.get_meta("planner_cost", 0.0))

    def events(self, task_id: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        if task_id:
            return self.conn.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit)).fetchall()
        return self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def efficiency(self) -> dict[str, dict[str, float]]:
        """Average cost/tokens per merged task, split by whether agents had the graph."""
        out = {}
        for used, n, cost, tok in self.conn.execute(
                "SELECT used_graph, COUNT(*), AVG(cost_usd), AVG(tokens) FROM tasks "
                "WHERE status='merged' GROUP BY used_graph"):
            out["with_graph" if used else "without_graph"] = {
                "tasks": n, "avg_cost_usd": round(cost or 0, 4), "avg_tokens": int(tok or 0)}
        return out

    def waiting(self) -> list[Task]:
        return [t for t in self.all() if t.status == "waiting"]

    def answer(self, task_id: str, answer: str, pick: int | None = None,
               scope_add: list[str] | None = None) -> Task:
        """Record the human's decision and send the task back to the queue to resume.

        `pick` is the chosen option (1-based); without it, an answer that is an option's text
        (alone or followed by " — note", as every answer channel writes a pick) counts as that
        option. Paths the picked option lists under the decision's scope_add, plus any
        `scope_add` given here, are added to the card's files_scope."""
        t = self.get(task_id)
        if t.status != "waiting" or not t.decision:
            raise ValueError(f"{task_id} is not waiting for a decision (status: {t.status})")
        q = t.decision.get("question", "")
        opts = t.decision.get("options", [])
        if pick is None:
            pick = next((i for i, o in enumerate(opts, 1)
                         if o and (answer == o or answer.startswith(o + " — "))), None)
        paths = list((t.decision.get("scope_add") or {}).get(str(pick), [])) if pick else []
        paths += [p for p in scope_add or [] if p not in paths]
        widened = [p for p in paths if p not in t.files_scope] if t.files_scope else []
        now = time.time()
        decision = {**t.decision, "answer": answer, "answered_at": now,
                    **({"scope_added": widened} if widened else {})}
        note = (f"The lead answered your question.\nQuestion: {q}\nAnswer: {answer}\n"
                + (f"Your files_scope now also includes: {', '.join(widened)}\n" if widened else "")
                + "Continue the task from where you stopped, applying this decision.")
        # Decisions live in their own list so every later attempt's card shows them.
        decisions = t.decisions + [{"question": q, "answer": answer, "at": now}]
        self.update(task_id, status="pending", decision=decision, decisions=decisions,
                    resume_note=note, attempts=max(t.attempts - 1, 0))  # asking is not a failed attempt
        self.log(task_id, "answered", answer)
        if widened:
            from .cards import widen_scope
            widen_scope(self, task_id, widened, "by answer")
        return self.get(task_id)

    def open_tasks(self) -> list[Task]:
        """Cards still in flight: pending, running, in review, or waiting on the human."""
        return [t for t in self.all() if t.status in OPEN_STATUSES]

    def unrunnable(self) -> list[Task]:
        """Pending tasks that can never run because a dependency is blocked/cancelled/missing."""
        tasks = {t.id: t for t in self.all()}
        dead = {i for i, t in tasks.items() if t.status in ("blocked", "cancelled")}
        changed = True
        while changed:  # propagate through the dependency graph
            changed = False
            for t in tasks.values():
                if t.status == "pending" and t.id not in dead and any(
                        d in dead or d not in tasks for d in t.depends_on):
                    dead.add(t.id)
                    changed = True
        return [tasks[i] for i in dead if tasks[i].status == "pending"]
