"""Phone notifications and answers through ntfy (https://ntfy.sh, or your own server).

Two topics: Orchestra publishes to `topic` (subscribe to it in the ntfy app) and reads your
answers from `reply_topic`. A question arrives with buttons for the recommended option and
one other; each button posts "T03 2" to the reply topic. "Other answer…" opens the reply
topic in the browser, where you can type "T03: your own words".

On public ntfy.sh the topic names are the only secret, so they are long, random and kept per
project in `.orchestra/ntfy.json` (gitignored), never in the committed orchestra.toml
(`orchestra ntfy setup`). A reply can only answer a card that is waiting for a decision, and a
button only answers the question it was sent with (each question carries a short tag).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import string
import urllib.parse
import urllib.request
from typing import Any

from .asking import scope_note
from .config import NtfySettings

MAX_ACTIONS = 3  # ntfy shows at most three buttons
REPLY_RE = re.compile(r"\s*([A-Za-z]{1,8}\d{1,6})(?:#([A-Za-z0-9]{4,8}))?(?![A-Za-z0-9])\s*[:\-—,.]?\s*(.*)", re.S)
REPLY_HELP = "Reply with: T03 2 (an option) · T03 r (the recommendation) · T03: your own answer"


def question_tag(repo: str, task_id: str, decision: dict) -> str:
    """Short id of one question in one project: a stale button can't answer a newer question."""
    return hashlib.sha1(f"{repo}:{task_id}:{decision.get('asked_at')}".encode()).hexdigest()[:6]


def random_topic() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "orchestra-" + "".join(secrets.choice(alphabet) for _ in range(16))


class Ntfy:
    def __init__(self, server: str, topic: str, reply_topic: str = "", token: str = "",
                 timeout: float = 15.0, label: str = ""):
        self.server = server.rstrip("/")
        self.topic, self.reply_topic, self.token, self.timeout = topic, reply_topic, token, timeout
        self.label = label  # project name shown in every title

    @classmethod
    def from_settings(cls, s: NtfySettings, label: str = "") -> "Ntfy | None":
        if not s.enabled:
            return None
        return cls(s.server, s.topic, s.reply_topic, os.environ.get(s.token_env, "") if s.token_env else "",
                   label=label)

    @property
    def can_receive(self) -> bool:
        """Replies need their own topic: reading our own messages back would loop."""
        return bool(self.reply_topic) and self.reply_topic != self.topic

    def reply_url(self) -> str:
        return f"{self.server}/{self.reply_topic}"

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    # ---------- blocking HTTP (run in a thread) ----------
    def _publish(self, payload: dict) -> dict:
        req = urllib.request.Request(self.server + "/", data=json.dumps(payload).encode(),
                                     headers=self._headers(), method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode() or "{}")

    def _poll(self, since: str) -> list[dict]:
        q = urllib.parse.urlencode({"poll": "1", "since": since})
        req = urllib.request.Request(f"{self.server}/{self.reply_topic}/json?{q}",
                                     headers={k: v for k, v in self._headers().items()
                                              if k == "Authorization"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            lines = r.read().decode().splitlines()
        msgs = [json.loads(line) for line in lines if line.strip()]
        return [m for m in msgs if m.get("event", "message") == "message"]

    # ---------- async API ----------
    async def publish(self, title: str, message: str, actions: list[dict] | None = None,
                      priority: int = 3, tags: list[str] | None = None, topic: str | None = None) -> dict:
        if self.label:
            title = f"{self.label} · {title}"
        payload: dict[str, Any] = {"topic": topic or self.topic, "title": title[:250],
                                   "message": message[:3800], "priority": priority}
        if actions:
            payload["actions"] = actions[:MAX_ACTIONS]
        if tags:
            payload["tags"] = tags
        return await asyncio.to_thread(self._publish, payload)

    async def poll(self, since: str | int) -> list[dict]:
        return await asyncio.to_thread(self._poll, str(since))

    # ---------- message shapes ----------
    def answer_action(self, label: str, body: str) -> dict:
        action = {"action": "http", "label": label[:40], "url": self.reply_url(), "method": "POST",
                  "body": body, "clear": True}
        if self.token:  # buttons post with the same access token on a protected server
            action["headers"] = {"Authorization": f"Bearer {self.token}"}
        return action

    def question(self, task_id: str, title: str, d: dict, tag: str = "") -> tuple[str, str, list[dict]]:
        """(title, message, actions) for a decision a card is waiting on. Answer buttons (and the
        reply help) only when replies can be read."""
        opts = d.get("options", [])
        rec = d.get("recommendation", "")
        lines = [d.get("question", "")]
        if d.get("why_it_matters"):
            lines += ["", f"Why it matters: {d['why_it_matters']}"]
        if opts:
            lines.append("")
            lines += [f"{i}. {o}{scope_note(d, i)}" for i, o in enumerate(opts, 1)]
        if rec:
            lines += ["", f"Recommended: {rec}"]
        actions: list[dict] = []
        if self.can_receive:
            lines += ["", REPLY_HELP.replace("T03", task_id)]
            ref = f"{task_id}#{tag}" if tag else task_id
            rec_idx = next((i for i, o in enumerate(opts, 1) if o == rec or (rec and rec.startswith(o))), None)
            if rec:
                label = f"✓ {rec_idx}. {opts[rec_idx - 1]}" if rec_idx else f"✓ {rec}"
                actions.append(self.answer_action(label, f"{ref} r"))
            other = next((i for i in range(1, len(opts) + 1) if i != rec_idx), None)
            if other is not None:
                actions.append(self.answer_action(f"{other}. {opts[other - 1]}", f"{ref} {other}"))
            actions.append({"action": "view", "label": "Other answer…", "url": self.reply_url(),
                            "clear": False})
        else:
            lines += ["", f"Answer with: orchestra answer {task_id} \"...\""]
        return f"{task_id} needs your decision · {title}"[:120], "\n".join(lines), actions[:MAX_ACTIONS]


def parse_reply(text: str) -> tuple[str, str, str] | None:
    """'T03 2' / 't03: use the 4h rule' / 'T03#a1b2c3 r' (a button) -> (card id, question tag or
    "", answer text); None if there is no card id."""
    m = REPLY_RE.fullmatch(text or "")
    if not m:
        return None
    return m.group(1).upper(), (m.group(2) or "").lower(), m.group(3).strip()
