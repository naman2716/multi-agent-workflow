"""Deterministic quality gates — the fail-fast pyramid.

Cheapest checks first: secrets, diff size, file scope, then the project's own
commands (lint, typecheck, tests). These never call a model; a gate failure
goes back to the worker as concrete feedback.
"""
from __future__ import annotations

import fnmatch
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config, Gate
from .gitops import DiffStats, matches_any, run, working_diff_stats

SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----"), "private key"),
    (re.compile(r"sk-(ant-|live_|proj-)?[A-Za-z0-9_\-]{20,}"), "API secret key"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "Google API key"),
    (re.compile(r"rzp_live_[A-Za-z0-9]{10,}"), "Razorpay live key"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"(?i)(password|passwd|secret|api_key|apikey|auth_token)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
     "hard-coded credential"),
]
ALWAYS_ALLOWED = ("*.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "pubspec.lock")


def find_secrets(text: str) -> list[str]:
    return sorted({label for pat, label in SECRET_PATTERNS if pat.search(text)})


def in_scope(path: str, scope: list[str]) -> bool:
    if not scope:
        return True
    if any(fnmatch.fnmatch(Path(path).name, p) for p in ALWAYS_ALLOWED):
        return True
    for pat in scope:
        pat = pat.rstrip("/")
        if fnmatch.fnmatch(path, pat) or path == pat or path.startswith(pat + "/"):
            return True
        if pat.endswith("/**") and path.startswith(pat[:-3]):
            return True
    return False


@dataclass
class GateResult:
    passed: bool
    name: str
    detail: str = ""


CONFLICT_MARKER = re.compile(r"^(<<<<<<<|>>>>>>>)( |$)", re.M)


# Exit codes a quick check uses for "nothing to run" rather than "tests failed":
# pytest 4 (usage / path not found) and 5 (no tests collected).
QUICK_INCONCLUSIVE = {4, 5}


async def run_gates(cfg: Config, wt: Path, stats: DiffStats, diff_text: str,
                    scope: list[str], env: dict | None = None, quick: bool = False,
                    exempt: list[str] | None = None) -> list[GateResult]:
    """`diff_text` must be the whole diff (not the reviewer's truncated excerpt): the secret
    scan reads every added line. `env` is the card's slot environment. With `quick` (fix
    rounds), a gate's `changed_cmd` runs first on the changed test files and fails fast.
    `exempt` files (a card's locked acceptance tests) don't count toward size or scope."""
    results: list[GateResult] = []
    exempt_set = set(exempt or [])

    added_lines = "\n".join(l[1:] for l in diff_text.splitlines()
                            if l.startswith("+") and not l.startswith("+++"))
    secrets = find_secrets(added_lines)
    results.append(GateResult(not secrets, "secrets",
                              f"possible secrets added: {', '.join(secrets)}" if secrets else ""))
    if secrets:
        return results

    markers = [m.group(0) for m in CONFLICT_MARKER.finditer(added_lines)]
    results.append(GateResult(not markers, "conflict_markers",
                              f"{len(markers)} merge conflict marker line(s) left in the change; "
                              "resolve every <<<<<<< / ======= / >>>>>>> block" if markers else ""))
    if markers:
        return results

    counted = stats.counted(list(cfg.diff_exclude) + sorted(exempt_set))
    skipped = f" (+{stats.lines - counted} in lockfiles/generated files, not counted)" \
        if counted != stats.lines else ""
    too_big = counted > cfg.max_diff_lines
    results.append(GateResult(not too_big, "diff_size",
                              f"{counted} changed lines exceeds limit of {cfg.max_diff_lines}{skipped}; "
                              "do only what the task asks, or report blocked so it can be split"
                              if too_big else f"{counted} lines{skipped}"))
    if too_big:
        return results

    if cfg.enforce_file_scope and scope:
        outside = [f for f in stats.files if not in_scope(f, scope) and f not in exempt_set]
        results.append(GateResult(not outside, "file_scope",
                                  f"edited files outside task scope {scope}: {outside}"
                                  if outside else ""))
        if outside:
            return results

    # quick checks only make sense on test files the change touched
    changed_tests = [f for f in stats.files if (wt / f).exists() and matches_any(f, cfg.test_globs)]
    for gate in cfg.gates:
        if quick and gate.changed_cmd and changed_tests:
            quick_cmd = gate.changed_cmd.replace("{files}", " ".join(shlex.quote(f) for f in changed_tests))
            rc, out = await run(quick_cmd, wt, timeout=gate.timeout_s, check=False, env=env)
            if rc != 0 and rc not in QUICK_INCONCLUSIVE:
                results.append(GateResult(False, f"{gate.name} (changed files)",
                                          f"exit {rc}\n{out[-3000:]}"))
                break
        rc, out = await run(gate.cmd, wt, timeout=gate.timeout_s, check=False, env=env)
        tail = out[-3000:]
        results.append(GateResult(rc == 0, gate.name, "" if rc == 0 else f"exit {rc}\n{tail}"))
        if rc != 0:
            break  # fail fast: no point running tests if lint failed
    return results


def summarize_failures(results: list[GateResult]) -> str:
    return "\n\n".join(f"[gate:{r.name}] FAILED\n{r.detail}" for r in results if not r.passed)


# ---------------------------------------------------------------- optional Stop-hook pre-check
STOP_GATE_REASON = ("Before you finish: these checks fail on your change, and the orchestrator "
                    "runs them next. Fix them, then give your final report again.\n\n")


def find_report(input_data: dict[str, Any], tail_lines: int = 400) -> dict | None:
    """The worker's structured report for this turn, if we can find it: the Stop input's
    `last_assistant_message` when it is the JSON report, else the last tool call in the
    transcript whose input looks like a report (has `status` and `files_changed`)."""
    msg = input_data.get("last_assistant_message")
    if isinstance(msg, str):
        try:
            data = json.loads(msg)
        except ValueError:
            data = None
        if isinstance(data, dict) and "status" in data:
            return data
    path = input_data.get("transcript_path")
    if not path or not Path(path).exists():
        return None
    for line in reversed(Path(path).read_text(errors="replace").splitlines()[-tail_lines:]):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) else None
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            inp = block.get("input") if isinstance(block, dict) else None
            if block.get("type") == "tool_use" and isinstance(inp, dict) \
                    and "status" in inp and "files_changed" in inp:
                return inp
    return None


SIZE_WARN_AT = 0.8
SIZE_TOOLS = "Write|Edit|MultiEdit|NotebookEdit|Bash"


def make_size_guard(worktree: Path, base: str, limit: int, exclude: list[str]):
    """Agent SDK PostToolUse hook for workers: count the change as the diff_size gate will, and
    tell the worker once it nears the limit and again once it passes it, so an oversized card
    stops early (report blocked, get split) instead of being written in full and then trimmed."""
    state = {"warned": 0.0}

    async def hook(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        try:
            counted = (await working_diff_stats(worktree, base)).counted(exclude)
        except Exception:  # a broken size check must never get in the worker's way
            return {}
        if counted > limit and state["warned"] < 1.0:
            state["warned"] = 1.0
            msg = (f"Size check: your change is now {counted} changed lines; the orchestrator rejects "
                   f"more than {limit}. Stop here and don't trim to fit: report status \"blocked\" "
                   "with the work done so far and how you would split this card into smaller ones.")
        elif counted > limit * SIZE_WARN_AT and state["warned"] < SIZE_WARN_AT:
            state["warned"] = SIZE_WARN_AT
            msg = (f"Size check: your change is {counted} changed lines, near the {limit}-line limit. "
                   "Finish within it; if the card can't fit, report \"blocked\" with how to split it.")
        else:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}
    return hook


def make_stop_gate(gates: list[Gate], worktree: Path, max_blocks: int = 1, env: dict | None = None):
    """Agent SDK Stop hook: when the worker reports `done`, run the fast gates in its worktree
    and, if one fails, send it back (at most `max_blocks` times). Never blocks on its own
    errors, on a non-`done` report, or when it can't find the report."""
    state = {"blocks": 0}

    async def hook(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        try:
            if state["blocks"] >= max_blocks:
                return {}
            report = find_report(input_data)
            if not report or report.get("status") != "done":
                return {}
            for g in gates:
                rc, out = await run(g.cmd, worktree, timeout=g.timeout_s, check=False, env=env)
                if rc != 0:
                    state["blocks"] += 1
                    return {"decision": "block",
                            "reason": STOP_GATE_REASON + f"[gate:{g.name}] `{g.cmd}` exit {rc}\n{out[-2500:]}"}
            return {}
        except Exception:  # a broken pre-check must never trap the worker
            return {}
    return hook
