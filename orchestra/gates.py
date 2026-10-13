"""Deterministic quality gates — the fail-fast pyramid.

Cheapest checks first: secrets, diff size, file scope, then the project's own
commands (lint, typecheck, tests). These never call a model; a gate failure
goes back to the worker as concrete feedback.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .gitops import DiffStats, run

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


async def run_gates(cfg: Config, wt: Path, stats: DiffStats, diff_text: str,
                    scope: list[str]) -> list[GateResult]:
    results: list[GateResult] = []

    added_lines = "\n".join(l[1:] for l in diff_text.splitlines()
                            if l.startswith("+") and not l.startswith("+++"))
    secrets = find_secrets(added_lines)
    results.append(GateResult(not secrets, "secrets",
                              f"possible secrets added: {', '.join(secrets)}" if secrets else ""))
    if secrets:
        return results

    too_big = stats.lines > cfg.max_diff_lines
    results.append(GateResult(not too_big, "diff_size",
                              f"{stats.lines} changed lines exceeds limit of {cfg.max_diff_lines}; "
                              "do only what the task asks, or report blocked so it can be split"
                              if too_big else f"{stats.lines} lines"))
    if too_big:
        return results

    if cfg.enforce_file_scope and scope:
        outside = [f for f in stats.files if not in_scope(f, scope)]
        results.append(GateResult(not outside, "file_scope",
                                  f"edited files outside task scope {scope}: {outside}"
                                  if outside else ""))
        if outside:
            return results

    for gate in cfg.gates:
        rc, out = await run(gate.cmd, wt, timeout=gate.timeout_s, check=False)
        tail = out[-3000:]
        results.append(GateResult(rc == 0, gate.name, "" if rc == 0 else f"exit {rc}\n{tail}"))
        if rc != 0:
            break  # fail fast: no point running tests if lint failed
    return results


def summarize_failures(results: list[GateResult]) -> str:
    return "\n\n".join(f"[gate:{r.name}] FAILED\n{r.detail}" for r in results if not r.passed)
