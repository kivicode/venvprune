"""Tree rendering of the prune decision over the venv's package hierarchy."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from venvprune.analyzer import Analysis
from venvprune.model import EdgeKind, ModuleInfo, Origin

_MARK = {
    EdgeKind.EAGER: ("+", "32"),
    EdgeKind.REEXPORT: ("~", "36"),
    EdgeKind.LAZY: ("?", "33"),
    EdgeKind.TYPE_ONLY: ("t", "35"),
}
_PRUNE_MARK = ("-", "31")

_LEGEND = "legend: + eager  ~ re-export only  ? lazy only  t type-only  - prunable"


@dataclass
class Node:
    name: str
    label: str
    status: EdgeKind | None = None
    """`None` means prunable."""

    size: int = 0
    info: ModuleInfo | None = None
    children: dict[str, Node] = field(default_factory=dict)

    @property
    def total_size(self) -> int:
        return self.size + sum(c.total_size for c in self.children.values())

    @property
    def counts(self) -> tuple[int, int]:
        """(prunable modules, total modules) in this subtree, self included."""
        pruned = 1 if (self.info is not None and self.status is None) else 0
        total = 1 if self.info is not None else 0
        for child in self.children.values():
            cp, ct = child.counts
            pruned += cp
            total += ct
        return pruned, total

    @property
    def fully_prunable(self) -> bool:
        pruned, total = self.counts
        return total > 0 and pruned == total


def build_tree(analysis: Analysis) -> Node:
    root = Node(name="", label="<venv>")
    for name, info in sorted(analysis.modules.items()):
        if info.origin is not Origin.SITE:
            continue
        node = root
        parts = name.split(".")
        for depth, part in enumerate(parts):
            node = node.children.setdefault(part, Node(name=".".join(parts[: depth + 1]), label=part))
        node.info = info
        node.status = analysis.reach.reached.get(name)
        if info.path.is_file():
            node.size = info.path.stat().st_size
    return root


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}G"


def _paint(text: str, color: str, enabled: bool) -> str:
    return f"\033[{color}m{text}\033[0m" if enabled else text


def render_tree(
    analysis: Analysis,
    max_depth: int = 3,
    only_prunable: bool = False,
    color: bool | None = None,
) -> str:
    if color is None:
        color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    root = build_tree(analysis)
    lines = [_LEGEND, ""]
    children = sorted(root.children.values(), key=lambda n: (-n.total_size, n.label))
    if only_prunable:
        children = [c for c in children if c.counts[0]]
    for index, child in enumerate(children):
        _render_node(child, "", index == len(children) - 1, 1, max_depth, only_prunable, color, lines)
    pruned, total = root.counts
    lines.append("")
    lines.append(f"{pruned}/{total} modules prunable ({_human(analysis.unused_bytes())} reclaimable)")
    return "\n".join(lines)


def _render_node(
    node: Node,
    prefix: str,
    last: bool,
    depth: int,
    max_depth: int,
    only_prunable: bool,
    color: bool,
    out: list[str],
) -> None:
    mark, hue = _PRUNE_MARK if node.status is None else _MARK[node.status]
    if node.info is None:
        mark, hue = ("/", "0")  # a directory with no module of its own (namespace package)
    connector = "└── " if last else "├── "
    pruned, total = node.counts
    detail = f"  {_human(node.total_size)}"
    if node.children:
        detail += f"  [{pruned}/{total} prunable]"
    label = _paint(f"{mark} {node.label}", hue, color and hue != "0")
    out.append(f"{prefix}{connector}{label}{detail}")

    child_prefix = prefix + ("    " if last else "│   ")
    if node.fully_prunable and node.children:
        out.append(f"{child_prefix}└── {_paint(f'({total} modules, all prunable)', '31', color)}")
        return
    if depth >= max_depth:
        if node.children:
            out.append(f"{child_prefix}└── … {len(node.children)} more")
        return

    children = sorted(node.children.values(), key=lambda n: (-n.total_size, n.label))
    if only_prunable:
        children = [c for c in children if c.counts[0]]
    for index, child in enumerate(children):
        _render_node(child, child_prefix, index == len(children) - 1, depth + 1, max_depth, only_prunable, color, out)
