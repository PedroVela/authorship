#!/usr/bin/env python3
"""Read-only MCP server over the authorship index (stdio, standard library only).

Speaks the Model Context Protocol's stdio transport: one JSON-RPC 2.0 message
per line on stdin and stdout. It implements what a tools-only server needs
(initialize, ping, tools/list, tools/call) and nothing else, so it runs on the
same python3 as the hooks, with nothing to install.

Seven tools, none of which writes evidence. Queries refresh the derived index
(.authorship/index.sqlite) so results include the latest entries; the ledger
itself is only read.
"""
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger  # noqa: E402
import queries  # noqa: E402

SERVER = {"name": "authorship", "version": ledger.plugin_version() or "0"}
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = ("Read-only access to this project's authorship ledger. Use it to recall prior ideas, decisions and "
                "discarded approaches. Cite entries as #<seq>. Nothing here can write to the ledger.")
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
SEQ = {"type": "string", "description": 'Entry or sub-node id, e.g. "4", or "3.2" for option 2 of reply #3.'}


def _tool(name, description, properties=None, required=()):
    return {"name": name, "description": description, "annotations": READ_ONLY,
            "inputSchema": {"type": "object", "properties": properties or {}, "required": list(required),
                            "additionalProperties": False}}


TOOLS = [
    _tool("chain_status", "Ledger integrity: entry count, head hash, whether the chain verifies, where it breaks, "
                          "and how much is sealed by external anchors."),
    _tool("search", 'Full-text search over ledger entries. Optional filters: tags (e.g. ["#idea"]), actor '
                    '("human" | "ai" | "system").',
          {"query": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
           "actor": {"type": "string", "enum": ["human", "ai", "system"]},
           "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}, ["query"]),
    _tool("get_node", "One entry or sub-node, with annotations, confirmed labels and edges.", {"seq": SEQ}, ["seq"]),
    _tool("lineage", "Ancestor subgraph of a node (usually a claim) with authors, and a summary of human- vs "
                     "AI-originated ancestors.",
          {"seq": SEQ, "depth": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
           "confirmed_only": {"type": "boolean", "default": False}}, ["seq"]),
    _tool("open_ideas", "Human ideas and hypotheses that were not adopted, superseded or discarded."),
    _tool("discarded", "Discarded or rejected approaches, each with the entry that gives the reason."),
    _tool("milestones", "Milestones (conception, decisions, reduction-to-practice evidence, AI-origin elements, "
                        "claim candidates) with tier and confirmation state.",
          {"since_seq": {"type": "integer", "minimum": 0}}),
]


def call_tool(name, args):
    store = ledger.Store(ledger.project_dir())
    if not store.exists():
        raise ValueError("authorship is not initialized in this project (run /authorship:init)")
    q = queries.Q(store)
    a = args or {}
    if name == "chain_status":
        return q.chain_status()
    if name == "search":
        return q.search(a.get("query") or "", a.get("tags"), a.get("actor"), a.get("limit", 20))
    if name == "get_node":
        return q.get_node(str(a["seq"]))
    if name == "lineage":
        return q.lineage(str(a["seq"]), int(a.get("depth", 10)), bool(a.get("confirmed_only", False)))
    if name == "open_ideas":
        return q.open_ideas()
    if name == "discarded":
        return q.discarded()
    if name == "milestones":
        return q.milestones(a.get("since_seq"))
    raise KeyError(name)


def handle(msg):
    """One JSON-RPC message in, one response out (None for notifications)."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notifications (initialized, cancelled ...) need no answer
    params = msg.get("params") or {}
    try:
        if method == "initialize":
            asked = params.get("protocolVersion")
            result = {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                      "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER,
                      "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = params.get("name")
            if name not in {t["name"] for t in TOOLS}:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool %r" % name}}
            try:
                data = call_tool(name, params.get("arguments"))
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                          "structuredContent": data, "isError": False}
            except Exception as exc:  # a tool error is a result, so the model can read it
                result = {"content": [{"type": "text", "text": "error: %s" % exc}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}}


def main():
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            out.flush()
            continue
        batch = msg if isinstance(msg, list) else [msg]
        replies = [r for r in (handle(m) for m in batch if isinstance(m, dict)) if r is not None]
        for r in replies:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
