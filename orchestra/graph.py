"""Graphify integration: a code knowledge graph kept in sync with the integration branch.

How it stays current
  A dedicated detached worktree (<worktree_root>/_graph) tracks the integration
  head. After every merge the orchestrator moves it to the new head and runs
  `graphify update` (tree-sitter AST only: local, no LLM tokens, incremental).

How agents use it
  Each task gets a frozen snapshot of graph.json taken when its worktree is
  created, so the graph always matches the code the agent sees. Agents query it
  through Graphify's MCP server (query_graph, get_node, get_neighbors,
  shortest_path, ...) instead of grepping and reading whole files.

What the orchestrator computes from it (deterministically, no model calls)
  - coupling between task scopes, so the planner serializes tasks that call into
    each other's files, not just tasks that share files;
  - impact of a diff (callers outside the change), handed to the reviewer.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .gitops import git, run

COUPLING_RELATIONS = {"calls", "indirect_call", "inherits", "extends", "implements", "mixes_in", "embeds"}
GRAPH_TOOLS = ["query_graph", "get_node", "get_neighbors", "get_community",
               "god_nodes", "graph_stats", "shortest_path"]
GRAPH_GUIDE = """
## Code knowledge graph
You have a code knowledge graph of this repository via the mcp__graph__* tools. It is
cheaper than reading files: use it FIRST to find where things live and what depends on what.
- query_graph(question) to locate relevant symbols/files; get_node(label) for one symbol;
  get_neighbors(label) for callers/callees; shortest_path(a, b) to see how two parts connect.
- Then Read only the specific files/line ranges you need. Avoid broad Grep/Glob sweeps.
- Edges tagged INFERRED or AMBIGUOUS are hints, not facts. Always Read a file before editing it.
"""


def graphify_available(python: str | None = None) -> bool:
    if python and python != sys.executable:
        return shutil.which(python) is not None
    return importlib.util.find_spec("graphify") is not None


@dataclass
class GraphSettings:
    enabled: bool = False
    python: str = ""  # interpreter with graphifyy[mcp] installed; default: this one
    refresh_timeout_s: int = 900

    @property
    def interpreter(self) -> str:
        return self.python or sys.executable


@dataclass
class GraphData:
    """Light in-memory view of graph.json for deterministic analysis."""
    node_file: dict[str, str] = field(default_factory=dict)
    node_label: dict[str, str] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "GraphData":
        g = json.loads(path.read_text())
        d = cls()
        for n in g.get("nodes", []):
            d.node_file[n["id"]] = n.get("source_file") or ""
            d.node_label[n["id"]] = n.get("label") or n["id"]
        for e in g.get("links", g.get("edges", [])):
            d.edges.append((e["source"], e["target"], e.get("relation", "")))
        return d

    def _nodes_in(self, scope: list[str]) -> set[str]:
        from .gates import in_scope
        return {n for n, f in self.node_file.items() if f and in_scope(f, scope)}

    def coupled(self, scope_a: list[str], scope_b: list[str]) -> list[str]:
        """Call/inheritance edges crossing between two scopes (either direction)."""
        if not scope_a or not scope_b:
            return []
        a, b = self._nodes_in(scope_a), self._nodes_in(scope_b)
        hits = []
        for s, t, rel in self.edges:
            if rel in COUPLING_RELATIONS and ((s in a and t in b) or (s in b and t in a)):
                hits.append(f"{self.node_label.get(s, s)} -{rel}-> {self.node_label.get(t, t)}")
        return hits

    def impact(self, changed_files: list[str], limit: int = 40) -> list[str]:
        """Symbols outside the diff that call/extend symbols defined in changed files."""
        changed = set(changed_files)
        defined = {n for n, f in self.node_file.items() if f in changed}
        out: dict[str, set[str]] = defaultdict(set)
        for s, t, rel in self.edges:
            if rel in COUPLING_RELATIONS and t in defined:
                src_file = self.node_file.get(s, "")
                if src_file and src_file not in changed:
                    out[f"{self.node_label.get(t, t)} ({self.node_file.get(t)})"].add(
                        f"{self.node_label.get(s, s)} [{src_file}]")
        lines = [f"- {target} is used by: {', '.join(sorted(srcs)[:6])}"
                 + (f" (+{len(srcs) - 6} more)" if len(srcs) > 6 else "")
                 for target, srcs in sorted(out.items(), key=lambda kv: -len(kv[1]))]
        return lines[:limit]


class Graph:
    def __init__(self, repo: Path, worktree_root: Path, integration: str, state_dir: Path,
                 settings: GraphSettings, log=print):
        self.repo, self.integration, self.settings, self.log = repo, integration, settings, log
        self.wt = worktree_root / "_graph"
        self.snap_dir = state_dir / "graph" / "snapshots"
        self._lock = asyncio.Lock()
        self.head: str | None = None
        self.error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.enabled and self.error is None

    @property
    def graph_json(self) -> Path:
        return self.wt / "graphify-out" / "graph.json"

    async def _exclude_output(self) -> None:
        """Make graphify-out/ invisible to git in every worktree (info/exclude is shared)."""
        common = Path(await git(self.repo, "rev-parse", "--git-common-dir"))
        exclude = (common if common.is_absolute() else self.repo / common) / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        lines = exclude.read_text().splitlines() if exclude.exists() else []
        for pat in ("graphify-out/", ".graphify_*"):
            if pat not in lines:
                lines.append(pat)
        exclude.write_text("\n".join(lines) + "\n")

    async def refresh(self) -> bool:
        """Move the graph worktree to the integration head and update the graph."""
        if not self.settings.enabled:
            return False
        async with self._lock:
            try:
                head = await git(self.repo, "rev-parse", self.integration)
                if head == self.head and self.graph_json.exists():
                    return True
                await self._exclude_output()
                if not (self.wt / ".git").exists():
                    self.wt.parent.mkdir(parents=True, exist_ok=True)
                    await git(self.repo, "worktree", "prune")
                    await git(self.repo, "worktree", "add", "--detach", str(self.wt), head)
                else:
                    await git(self.wt, "checkout", "-q", "--detach", head)
                started = asyncio.get_running_loop().time()
                rc, out = await run([self.settings.interpreter, "-m", "graphify", "update", ".", "--force"],
                                    self.wt, timeout=self.settings.refresh_timeout_s, check=False)
                if rc != 0 or not self.graph_json.exists():
                    raise RuntimeError(f"graphify update failed ({rc}): {out[-800:]}")
                self.head = head
                took = asyncio.get_running_loop().time() - started
                stats = next((l for l in out.splitlines() if "nodes" in l and "edges" in l), "").strip()
                self.log(f"graph refreshed at {head[:8]} in {took:.1f}s {stats}")
                return True
            except Exception as e:  # never let the graph take the run down
                self.error = str(e)[:500]
                self.log(f"graph disabled for this run: {self.error}")
                return False

    async def snapshot(self, task_id: str) -> Path | None:
        """Bring the graph up to the integration head, then freeze a copy for one agent."""
        if not self.enabled or not await self.refresh():
            return None
        async with self._lock:  # never copy while graphify is writing
            self.snap_dir.mkdir(parents=True, exist_ok=True)
            dst = self.snap_dir / f"{task_id}.json"
            shutil.copyfile(self.graph_json, dst)
            return dst

    def data(self, path: Path | None = None) -> GraphData | None:
        p = path or self.graph_json
        try:
            return GraphData.load(p) if p and p.exists() else None
        except (OSError, json.JSONDecodeError, KeyError):
            return None

    def mcp_config(self, graph_path: Path) -> dict:
        return {"graph": {"type": "stdio", "command": self.settings.interpreter,
                          "args": ["-m", "graphify.serve", str(graph_path)]}}

    async def close(self) -> None:
        if self.wt.exists():
            await git(self.repo, "worktree", "remove", "--force", str(self.wt), check=False)


def mcp_tool_names() -> list[str]:
    return [f"mcp__graph__{t}" for t in GRAPH_TOOLS]

