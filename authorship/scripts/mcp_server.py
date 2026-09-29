# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp==2.2.0"]
# ///
"""Read-only MCP server over the authorship index (stdio).

Run with `uv run --script mcp_server.py`. Seven tools, none of which writes
evidence. Queries refresh the derived index (.authorship/index.sqlite) so
results include the latest entries; the ledger itself is only read.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

import ledger  # noqa: E402
import queries  # noqa: E402

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

server = MCPServer(
    name="authorship",
    instructions=(
        "Read-only access to this project's authorship ledger. Use it to recall prior ideas, decisions and "
        "discarded approaches. Cite entries as #<seq>. Nothing here can write to the ledger."
    ),
)


def _q():
    store = ledger.Store(ledger.project_dir())
    if not store.exists():
        raise RuntimeError("authorship is not initialized in this project (run /authorship:init)")
    return queries.Q(store)


@server.tool(annotations=READ_ONLY)
def chain_status() -> dict:
    """Ledger integrity: entry count, head hash, whether the chain verifies, where it breaks, and how much is sealed by external anchors."""
    return _q().chain_status()


@server.tool(annotations=READ_ONLY)
def search(query: str, tags: list[str] | None = None, actor: str | None = None, limit: int = 20) -> dict:
    """Full-text search over ledger entries. Optional filters: tags (e.g. ["#idea"]), actor ("human" | "ai" | "system")."""
    return _q().search(query, tags, actor, limit)


@server.tool(annotations=READ_ONLY)
def get_node(seq: str) -> dict:
    """One entry or sub-node (e.g. "4", or "3.2" for option 2 of response #3), with annotations, confirmed labels and edges."""
    return _q().get_node(seq)


@server.tool(annotations=READ_ONLY)
def lineage(seq: str, depth: int = 10, confirmed_only: bool = False) -> dict:
    """Ancestor subgraph of a node (usually a #claim) with authors, and a summary of human- vs AI-originated ancestors."""
    return _q().lineage(seq, depth, confirmed_only)


@server.tool(annotations=READ_ONLY)
def open_ideas() -> dict:
    """Human ideas and hypotheses that were not adopted, superseded or discarded."""
    return _q().open_ideas()


@server.tool(annotations=READ_ONLY)
def discarded() -> dict:
    """Discarded or rejected approaches, each with the entry that gives the reason."""
    return _q().discarded()


@server.tool(annotations=READ_ONLY)
def milestones(since_seq: int | None = None) -> dict:
    """Milestones (conception, decisions, reduction-to-practice evidence, AI-origin elements, claim candidates) with tier and confirmation state."""
    return _q().milestones(since_seq)


if __name__ == "__main__":
    server.run("stdio")
