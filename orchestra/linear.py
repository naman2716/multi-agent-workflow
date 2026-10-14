"""Read a Linear ticket as plan input (and optionally comment on it).

Nothing here runs on its own: a ticket is only read when you run
`orchestra plan --linear ENG-123` or `orchestra linear show ENG-123`.
The API key comes from an environment variable (LINEAR_API_KEY by default).
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from .config import LinearSettings

API_URL = "https://api.linear.app/graphql"
IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,9}-\d+")

ISSUE_QUERY = """query Issue($id: String!) {
  issue(id: $id) {
    id identifier title description url
    comments(first: 50) { nodes { body createdAt user { name } } }
  }
}"""
COMMENT_MUTATION = """mutation Comment($input: CommentCreateInput!) {
  commentCreate(input: $input) { success }
}"""


class LinearError(RuntimeError):
    pass


def normalize_id(identifier: str) -> str:
    ident = (identifier or "").strip()
    if not IDENT_RE.fullmatch(ident):
        raise LinearError(f"{identifier!r} doesn't look like a Linear ticket id (e.g. ENG-123)")
    return ident.upper()


class Linear:
    def __init__(self, api_key: str, url: str = API_URL, timeout: float = 20.0):
        self.api_key, self.url, self.timeout = api_key, url, timeout

    @classmethod
    def from_settings(cls, s: LinearSettings) -> "Linear":
        key = os.environ.get(s.api_key_env, "")
        if not key:
            raise LinearError(f"set {s.api_key_env} to a Linear personal API key "
                              "(Linear → Settings → Security & access → Personal API keys)")
        return cls(key)

    def _query(self, query: str, variables: dict) -> dict:
        req = urllib.request.Request(self.url, data=json.dumps({"query": query, "variables": variables}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": self.api_key}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise LinearError(f"Linear API returned {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise LinearError(f"could not reach Linear: {e.reason}") from None
        except (OSError, ValueError) as e:  # timeouts, resets, a non-JSON reply
            raise LinearError(f"Linear request failed: {e!r}") from None
        if body.get("errors"):
            raise LinearError("Linear API error: " + "; ".join(e.get("message", "?") for e in body["errors"]))
        return body.get("data") or {}

    def issue(self, identifier: str) -> dict:
        ident = normalize_id(identifier)
        issue = self._query(ISSUE_QUERY, {"id": ident}).get("issue")
        if not issue:
            raise LinearError(f"ticket {ident} not found (or the API key can't see it)")
        nodes = (issue.get("comments") or {}).get("nodes") or []
        issue["comments"] = sorted(nodes, key=lambda c: c.get("createdAt") or "")
        return issue

    def comment(self, issue_id: str, body: str) -> None:
        data = self._query(COMMENT_MUTATION, {"input": {"issueId": issue_id, "body": body}})
        if not (data.get("commentCreate") or {}).get("success"):
            raise LinearError("Linear did not accept the comment")


def ticket_goal(issue: dict, notes: str = "") -> str:
    """The plan goal built from a ticket: title, link, description, comments, your notes."""
    parts = [f"Linear {issue['identifier']}: {issue.get('title', '').strip()}",
             issue.get("url", ""), "", (issue.get("description") or "(no description)").strip()]
    comments = [c for c in issue.get("comments", []) if (c.get("body") or "").strip()]
    if comments:
        parts += ["", "Comments on the ticket:"]
        for c in comments[-15:]:
            who = (c.get("user") or {}).get("name") or "someone"
            body = c["body"].strip()
            parts.append(f"- {who}: {body[:1500] + ' …' if len(body) > 1500 else body}")
    if notes.strip():
        parts += ["", "Notes from the lead (these take priority over the ticket):", notes.strip()]
    return "\n".join(parts).strip()
