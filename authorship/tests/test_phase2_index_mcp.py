import json
import os
import shutil
import subprocess
import sys

import pytest

import harness
import index
import ledger
import queries
from harness import SCRIPTS

TOOLS = {"chain_status", "search", "get_node", "lineage", "open_ideas", "discarded", "milestones"}


def test_fixture_lineage_matches_spec(qr_confirmed):
    q = queries.Q(qr_confirmed)
    anc = index.lineage(q.conn, "13", confirmed_only=True)
    assert set(anc) == {"2", "4", "11", "13", "3.3"}
    res = q.lineage("13", confirmed_only=True)
    s = res["summary"]
    assert s["ai_originated"] == 1 and s["human_originated"] == 3
    ai = s["ai_elements"][0]
    assert ai["node"] == "3.3" and ai["seq"] == 3 and ai["modified_by_human_at"] == ["4"]
    assert "TTL cache" in ai["text"]


def test_fixture_stages(qr):
    conn = index.update(qr)
    rows = conn.execute("SELECT stage, MIN(seq), MAX(seq) FROM entries WHERE stage IS NOT NULL GROUP BY stage ORDER BY 2").fetchall()
    assert [tuple(r) for r in rows] == [("Exploration", 2, 10), ("Prototype", 11, 13)]


def test_fixture_nodes_and_rule_edges(qr):
    conn = index.update(qr)
    nodes = {r["node_id"]: dict(r) for r in conn.execute("SELECT * FROM nodes")}
    assert nodes["2"]["ibis_type"] == "issue"
    assert [nodes["3.%d" % i]["author"] for i in (1, 2, 3)] == ["ai"] * 3
    assert nodes["4"]["ibis_type"] == "position" and nodes["4"]["author"] == "human"
    assert nodes["10"]["status"] == "discarded"
    assert nodes["11"]["ibis_type"] == "decision" and nodes["13"]["ibis_type"] == "claim"
    rule = {(r["src"], r["type"], r["dst"]) for r in conn.execute("SELECT * FROM edges WHERE source='rule'")}
    assert {("5", "implements", "4"), ("7", "implements", "4"), ("12", "implements", "11"),
            ("6", "objects_to", "4"), ("8", "supports", "4")} <= rule


def _dump(conn):
    out = {}
    for t in ("entries", "nodes", "edges", "confirmations", "annotations", "milestones"):
        out[t] = sorted(tuple(r) for r in conn.execute("SELECT * FROM %s" % t))
    out["fts"] = sorted(tuple(r) for r in conn.execute("SELECT rowid, text FROM fts"))
    return out


def test_rebuild_and_incremental_are_identical(project):
    calls = []

    def step(store):
        index.update(store).close()
        calls.append(1)

    store = harness.replay(project, on_step=step)
    harness.confirm_edges(store, harness.load_edges()[:5])
    index.update(store).close()
    harness.confirm_edges(store, harness.load_edges()[5:])
    with open(store.annotations, "a") as f:
        f.write(json.dumps({"id": "ann_x", "ts": "2026-01-01T00:00:00.000Z", "target_seq": 4, "model": "jev-1.13.0",
                            "questions_hash": "q", "answers": {"maturity": {"choice": "mechanism"}},
                            "milestones": [{"type": "conception_candidate", "tier": 1, "score": 0.83}],
                            "edges": [{"src": "4", "dst": "3.3", "type": "modifies", "p": 0.81}], "supersedes": None}) + "\n")
    incremental = _dump(index.update(store))
    assert len(calls) > 10
    rebuilt = _dump(index.update(store, rebuild=True))
    assert incremental == rebuilt
    assert len(rebuilt["entries"]) == 26


def test_every_result_item_carries_seq_and_hash(qr_confirmed):
    q = queries.Q(qr_confirmed)
    results = [q.chain_status(), q.search("sequence"), q.search("", tags=["#idea"]), q.get_node("4"), q.get_node("3.3"),
               q.lineage("13"), q.lineage("13", confirmed_only=True), q.open_ideas(), q.discarded(), q.milestones()]

    def check(obj):
        if isinstance(obj, dict):
            if any(k in obj for k in ("node", "event", "type", "preview")) or obj is results[0]:
                assert "seq" in obj and "hash" in obj, obj
                assert obj["hash"] is None or len(obj["hash"]) in (12, 64)  # full hash only inside get_node.entry
            for v in obj.values():
                check(v)
        elif isinstance(obj, list):
            for v in obj:
                check(v)

    for r in results:
        check(r)
    assert q.search("sequence")["items"]
    assert [i["seq"] for i in q.discarded()["items"]] == [3, 10]  # 3.2 rejected by #11, #10 discarded


def test_twenty_kb_cap(project):
    store = harness.init_store(project)
    for i in range(120):
        ledger.write_note(store, "#idea variant %d of the counter method " % i + "detail " * 300)
    q = queries.Q(store)
    res = q.search("counter", limit=100)
    assert res["truncated"] is True
    assert len(json.dumps(res, ensure_ascii=False).encode("utf-8")) <= 20 * 1024
    assert queries.cap({"items": [], "big": "x" * 30000})["truncated"] is True


def _uv():
    return shutil.which("uv")


@pytest.mark.skipif(not _uv(), reason="uv not installed")
def test_mcp_server_lists_exactly_seven_read_only_tools(qr_confirmed):
    env = harness.hook_env(qr_confirmed.project)
    proc = subprocess.Popen([_uv(), "run", "--quiet", "--script", os.path.join(SCRIPTS, "mcp_server.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

    def rpc(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        if "id" not in msg:
            return None
        while True:
            line = proc.stdout.readline()
            assert line, proc.stderr.read()
            out = json.loads(line)
            if out.get("id") == msg["id"]:
                return out

    try:
        init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
        assert init["result"]["serverInfo"]["name"] == "authorship"
        rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        assert {t["name"] for t in tools} == TOOLS and len(tools) == 7
        for t in tools:
            assert t["annotations"]["readOnlyHint"] is True and t["annotations"]["destructiveHint"] is False
        before = open(qr_confirmed.ledger, "rb").read()
        res = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "lineage", "arguments": {"seq": "13", "confirmed_only": True}}})["result"]
        payload = res.get("structuredContent") or json.loads(res["content"][0]["text"])
        assert payload["summary"]["ai_elements"][0]["node"] == "3.3"
        res = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "chain_status", "arguments": {}}})["result"]
        payload = res.get("structuredContent") or json.loads(res["content"][0]["text"])
        assert payload["ok"] is True and payload["entries"] == 26
        assert open(qr_confirmed.ledger, "rb").read() == before
    finally:
        proc.kill()
        proc.wait()
