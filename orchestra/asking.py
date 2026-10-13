"""Asking the human in the terminal, live, while other agents keep working.

When a worker needs a decision, `orchestra run` shows the question right away (with a
bell) and reads the answer from the terminal. Log lines from other agents are held back
while the question is on screen so it stays readable, then printed after you answer.

Input is read on a daemon thread, so a pending prompt never blocks the agents or keeps
the process alive at exit.
"""
from __future__ import annotations

import asyncio
import re
import sys
import threading
from typing import Protocol

LATER = {"later", "l", "skip", "s"}
RECOMMENDED = {"r", "rec", "recommended", "y", "yes"}


class LineSource(Protocol):
    async def readline(self) -> str | None: ...   # None = input closed
    def drain(self) -> None: ...


class TerminalInput:
    def __init__(self, stream=None):
        self.stream = stream or sys.stdin
        self._q: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False

    def _start(self) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        self._q = asyncio.Queue()
        self._started = True

        def pump():
            for line in self.stream:
                self._loop.call_soon_threadsafe(self._q.put_nowait, line.rstrip("\r\n"))
            self._loop.call_soon_threadsafe(self._q.put_nowait, None)

        threading.Thread(target=pump, name="orchestra-stdin", daemon=True).start()

    async def readline(self) -> str | None:
        self._start()
        return await self._q.get()

    def drain(self) -> None:
        """Forget anything typed while no question was showing."""
        if self._q is not None:
            while not self._q.empty():
                self._q.get_nowait()


def render_question(task_id: str, title: str, d: dict, others_running: int) -> str:
    bar = "─" * 64
    lines = ["", f"\a{bar}", f"  ❓ {task_id} needs your decision  ·  {title}", bar,
             f"  {d.get('question', '')}"]
    if d.get("why_it_matters"):
        lines.append(f"  Why it matters: {d['why_it_matters']}")
    if d.get("work_so_far"):
        lines.append(f"  Work so far: {d['work_so_far'][:300]}")
    lines.append("")
    for i, opt in enumerate(d.get("options", []), 1):
        lines.append(f"   {i}. {opt}")
    if d.get("recommendation"):
        lines.append(f"  Agent recommends: {d['recommendation']}")
    lines += ["",
              "  Type a number, 'r' for the recommendation, your own answer,",
              "  or 'later' to leave it for now."
              + (f"  ({others_running} other agent(s) still working.)" if others_running else ""),
              bar]
    return "\n".join(lines)


def parse_answer(text: str, d: dict) -> tuple[str, str | None]:
    """-> ("answer", value) | ("later", None) | ("invalid", hint)"""
    t = (text or "").strip()
    if not t:
        return "invalid", "Type a number, 'r', your own answer, or 'later'."
    low = t.lower()
    if low in LATER:
        return "later", None
    opts = d.get("options", [])
    if low in RECOMMENDED:
        rec = d.get("recommendation")
        return ("answer", rec) if rec else ("invalid", "No recommendation was given; pick an option.")
    # "2" picks option 2; "2: but round up" / "2 - note" picks it with a note.
    # Anything else (e.g. "4 hours") is a free-text answer.
    m = re.fullmatch(r"(\d+)\s*(?:[.:,\-—]\s*(.*))?", t)
    if m:
        n, note = int(m.group(1)), (m.group(2) or "").strip()
        if 1 <= n <= len(opts):
            if not note:
                return "answer", opts[n - 1]
            return "answer", f"{opts[n - 1]} — with this change (overrides the option where they differ): {note}"
        return "invalid", f"Pick 1-{len(opts)}, or type your own answer."
    return "answer", t
