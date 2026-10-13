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
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
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
"""
MIGRATIONS = [  # columns added after v0.1; applied idempotently
    ("tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("used_graph", "INTEGER NOT NULL DEFAULT 0"),
    ("model", "TEXT"),
    ("session_id", "TEXT"),      # worker session, resumable after a decision or a limit pause
    ("base_commit", "TEXT"),     # integration head the worktree was cut from
    ("resume_note", "TEXT"),     # message to send when resuming that session
    ("decision", "TEXT"),        # JSON: the open (or answered) question for the human
]

JSON_FIELDS = ("acceptance", "non_goals", "files_scope", "depends_on", "feedback")
STATUSES = ("pending", "running", "review", "waiting", "merged", "blocked", "cancelled")


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

    # ---------- writes ----------
    def add_task(self, task: Task) -> None:
        now = time.time()
        with self._lock:
            seq = self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM tasks").fetchone()[0]
            self.conn.execute(
                """INSERT INTO tasks (id,title,spec,acceptance,non_goals,files_scope,depends_on,
                   complexity,model,status,attempts,feedback,created_at,updated_at,seq)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0,'[]',?,?,?)""",
                (task.id, task.title, task.spec, json.dumps(task.acceptance),
                 json.dumps(task.non_goals), json.dumps(task.files_scope),
                 json.dumps(task.depends_on), task.complexity, task.model, task.status,
                 now, now, seq),
            )
        self.log(task.id, "created", task.title)

    def ready_ids(self) -> list[str]:
        """Pending tasks whose dependencies are merged (read-only; does not claim)."""
        merged = {r[0] for r in self.conn.execute("SELECT id FROM tasks WHERE status='merged'")}
        return [t.id for t in self.all() if t.status == "pending" and all(d in merged for d in t.depends_on)]

    def claim_ready(self, limit: int) -> list[Task]:
        """Atomically claim up to `limit` pending tasks whose dependencies are all merged."""
        claimed: list[Task] = []
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM tasks WHERE status='pending' ORDER BY seq").fetchall()
            merged = {r[0] for r in self.conn.execute("SELECT id FROM tasks WHERE status='merged'")}
            for row in rows:
                if len(claimed) >= limit:
                    break
                t = Task.from_row(row)
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

    def add_cost(self, task_id: str, usd: float, tokens: int = 0) -> None:
        with self._lock:
            self.conn.execute("UPDATE tasks SET cost_usd=cost_usd+?, tokens=tokens+? WHERE id=?",
                              (usd or 0.0, tokens or 0, task_id))

    def recover_stale(self) -> list[str]:
        """After a crash, tasks left 'running'/'review' go back to pending."""
        with self._lock:
            ids = [r[0] for r in self.conn.execute(
                "SELECT id FROM tasks WHERE status IN ('running','review')")]
            self.conn.execute(
                "UPDATE tasks SET status='pending', attempts=MAX(attempts-1,0), session_id=NULL, "
                "resume_note=NULL, worktree=NULL WHERE status IN ('running','review')")
        for i in ids:
            self.log(i, "recovered", "reset to pending after interrupted run")
        return ids

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

    def answer(self, task_id: str, answer: str) -> Task:
        """Record the human's decision and send the task back to the queue to resume."""
        t = self.get(task_id)
        if t.status != "waiting" or not t.decision:
            raise ValueError(f"{task_id} is not waiting for a decision (status: {t.status})")
        q = t.decision.get("question", "")
        decision = {**t.decision, "answer": answer, "answered_at": time.time()}
        note = (f"The lead answered your question.\nQuestion: {q}\nAnswer: {answer}\n"
                "Continue the task from where you stopped, applying this decision.")
        self.update(task_id, status="pending", decision=decision, resume_note=note,
                    attempts=max(t.attempts - 1, 0))  # asking is not a failed attempt
        self.add_feedback(task_id, f"Decision from the lead — Q: {q} A: {answer}")
        self.log(task_id, "answered", answer)
        return self.get(task_id)

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
