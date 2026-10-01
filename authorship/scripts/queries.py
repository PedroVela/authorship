"""Read-only queries behind the MCP tools. Standard library only.

Every item carries `seq` and a short `hash` so Claude can cite it as #<seq>.
Results are capped at 20 KB, with `truncated: true` when items were dropped.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import index  # noqa: E402
import ledger  # noqa: E402
import rules  # noqa: E402

CAP_BYTES = 20 * 1024
HASH_CHARS = 12


def _size(obj):
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def cap(result, key="items"):
    """Drop trailing items (then shorten text) until the JSON fits in 20 KB."""
    result.setdefault("truncated", False)
    items = result.get(key)
    while _size(result) > CAP_BYTES and isinstance(items, list) and items:
        items.pop()
        result["truncated"] = True
    if _size(result) > CAP_BYTES:
        for k, v in list(result.items()):
            if isinstance(v, str) and len(v) > 400:
                result[k] = v[:400] + "…"
                result["truncated"] = True
    return result


class Q(object):
    def __init__(self, store, refresh=True):
        self.store = store
        self.conn = index.update(store) if refresh else index.connect(store)

    # helpers ---------------------------------------------------------------

    def _entry_row(self, seq):
        return self.conn.execute("SELECT * FROM entries WHERE seq=?", (seq,)).fetchone()

    def _hash(self, seq):
        r = self._entry_row(seq)
        return r["hash"][:HASH_CHARS] if r else None

    def _node(self, node_id, depth=None):
        n = self.conn.execute("SELECT * FROM nodes WHERE node_id=?", (str(node_id),)).fetchone()
        if not n:
            seq = rules.node_seq(node_id)
            r = self._entry_row(seq)
            if not r:
                return None
            item = {"node": str(node_id), "seq": seq, "hash": r["hash"][:HASH_CHARS], "event": r["event"],
                    "author": r["actor"], "text": rules.short(r["text"], 300)}
        else:
            item = {"node": n["node_id"], "seq": n["seq"], "hash": self._hash(n["seq"]), "ibis": n["ibis_type"],
                    "author": n["author"], "stage": n["stage"], "status": n["status"], "maturity": n["maturity"],
                    "text": n["label"]}
        if depth is not None:
            item["depth"] = depth
        return item

    def _preview(self, r):
        return {"seq": r["seq"], "hash": r["hash"][:HASH_CHARS], "ts": r["ts"], "event": r["event"], "actor": r["actor"],
                "tags": json.loads(r["tags_json"] or "[]"), "auto_tags": json.loads(r["auto_tags_json"] or "[]"),
                "stage": r["stage"], "file": r["file"],
                "preview": rules.short(r["text"], 240)}

    # tools -----------------------------------------------------------------

    def chain_status(self):
        v = ledger.verify(self.store)
        return {"entries": v["entries"], "head": (v["head"] or "")[:HASH_CHARS] or None, "ok": v["ok"],
                "broken_at": v["broken_at"], "reason": v["reason"], "sealed_upto": v["sealed_upto"],
                "unsealed": v["unsealed"], "seq": v["entries"], "hash": (v["head"] or "")[:HASH_CHARS] or None}

    def search(self, query="", tags=None, actor=None, limit=20):
        limit = max(1, min(int(limit or 20), 100))
        where, params = [], []
        if query and query.strip():
            terms = [t for t in re.findall(r"\w+", query, re.UNICODE) if t]
            if terms:
                where.append("e.seq IN (SELECT rowid FROM fts WHERE fts MATCH ?)")
                params.append(" ".join('"%s"' % t for t in terms))
        for t in tags or []:
            t = "#" + t.lstrip("#").lower()
            t = "#" + ledger.TAG_ALIASES.get(t[1:], t[1:])
            where.append("(e.tags_json LIKE ? OR e.auto_tags_json LIKE ?)")
            params += ['%%"%s"%%' % t] * 2
        if actor:
            where.append("e.actor = ?")
            params.append(actor)
        sql = "SELECT e.* FROM entries e %s ORDER BY e.seq DESC LIMIT ?" % ("WHERE " + " AND ".join(where) if where else "")
        rows = self.conn.execute(sql, params + [limit]).fetchall()
        return cap({"query": query, "items": [self._preview(r) for r in rows]})

    def get_node(self, seq):
        node_id = str(seq)
        seq = rules.node_seq(node_id)
        r = self._entry_row(seq)
        if not r:
            return {"error": "no entry #%s" % node_id, "seq": seq, "hash": None}
        entry = ledger.find_entry(self.store, seq) or {}
        full = {k: v for k, v in entry.items() if k not in ("text", "prev")}
        full["text"] = rules.short(r["text"], 4000)
        subs = [self._node(x["node_id"]) for x in
                self.conn.execute("SELECT node_id FROM nodes WHERE seq=? AND node_id != ? ORDER BY node_id", (seq, str(seq)))]
        anns = [{"id": a["id"], "model": a["model"], "questions_hash": a["questions_hash"],
                 "answers": json.loads(a["answers_json"]), "milestones": json.loads(a["milestones_json"]),
                 "superseded_by": a["superseded_by"], "seq": seq, "hash": r["hash"][:HASH_CHARS]}
                for a in self.conn.execute("SELECT * FROM annotations WHERE target_seq=? ORDER BY ts", (seq,))]
        confirmed = [{"seq": c["seq"], "hash": self._hash(c["seq"]), "decision": c["decision"], "label": c["label"],
                      "edited_label": c["edited_label"]}
                     for c in self.conn.execute("SELECT * FROM confirmations WHERE target_seq=? ORDER BY seq", (seq,))]
        ids = [node_id] + [s["node"] for s in subs if s]
        marks = ",".join("?" * len(ids))
        edges = [{"src": e["src"], "dst": e["dst"], "type": e["type"], "source": e["source"],
                  "seq": rules.node_seq(e["dst"] if e["src"] in ids else e["src"]),
                  "hash": self._hash(rules.node_seq(e["dst"] if e["src"] in ids else e["src"]))}
                 for e in self.conn.execute("SELECT * FROM edges WHERE src IN (%s) OR dst IN (%s) ORDER BY src, dst, type"
                                            % (marks, marks), ids + ids)]
        return cap({"seq": seq, "hash": r["hash"][:HASH_CHARS], "node": self._node(node_id), "entry": full,
                    "sub_nodes": subs, "annotations": anns, "confirmations": confirmed, "items": edges}, key="items")

    def lineage(self, seq, depth=10, confirmed_only=False, curated=False):
        anc = index.lineage(self.conn, str(seq), int(depth), bool(confirmed_only), bool(curated))
        nodes = [self._node(n, d) for n, d in sorted(anc.items(), key=lambda kv: (kv[1], rules.node_seq(kv[0]), kv[0]))]
        nodes = [n for n in nodes if n]
        ids = set(anc)
        edges = []
        for e in self.conn.execute("SELECT * FROM edges ORDER BY src, dst, type"):
            if e["src"] in ids and e["dst"] in ids and (not confirmed_only or e["source"] == "confirmed") \
                    and (not curated or e["source"] in ("confirmed", "auto")):
                edges.append({"src": e["src"], "dst": e["dst"], "type": e["type"], "source": e["source"],
                              "seq": rules.node_seq(e["src"]), "hash": self._hash(rules.node_seq(e["src"]))})
        ai = []
        for n in nodes:
            if n["author"] == "ai":
                mods = sorted({e["src"] for e in edges if e["dst"] == n["node"] and e["type"] in ("modifies", "refines")
                               and (self._node(e["src"]) or {}).get("author") == "human"}, key=rules.node_seq)
                ai.append({"node": n["node"], "seq": n["seq"], "hash": n["hash"], "text": n["text"],
                           "modified_by_human_at": mods})
        summary = {"human_originated": sum(1 for n in nodes if n["author"] == "human" and str(n["node"]) != str(seq)),
                   "ai_originated": len(ai), "ai_elements": ai}
        return cap({"seq": rules.node_seq(seq), "hash": self._hash(rules.node_seq(seq)), "root": str(seq),
                    "confirmed_only": bool(confirmed_only), "summary": summary, "items": nodes, "edges": edges})

    def open_ideas(self):
        rows = self.conn.execute(
            "SELECT n.node_id FROM nodes n JOIN entries e ON e.seq = n.seq WHERE n.author='human' AND n.status='open'"
            " AND n.option IS NULL AND (e.tags_json LIKE '%\"#idea\"%' OR e.tags_json LIKE '%\"#hypothesis\"%'"
            " OR e.auto_tags_json LIKE '%\"#idea\"%' OR e.auto_tags_json LIKE '%\"#hypothesis\"%')"
            " ORDER BY n.seq").fetchall()
        return cap({"items": [self._node(r["node_id"]) for r in rows]})

    def discarded(self):
        items = []
        for r in self.conn.execute("SELECT node_id, seq FROM nodes WHERE status IN ('discarded','rejected') ORDER BY seq, node_id"):
            n = self._node(r["node_id"])
            reason = self.conn.execute(
                "SELECT src FROM edges WHERE dst=? AND type IN ('discards','rejects','objects_to') AND source != 'annotation'"
                " ORDER BY source='confirmed' DESC, src LIMIT 1", (r["node_id"],)).fetchone()
            if reason:
                n["reason"] = self._node(reason["src"])
            else:
                n["reason"] = {"node": n["node"], "seq": n["seq"], "hash": n["hash"], "text": n["text"],
                               "note": "the discard entry states its own reason"}
            items.append(n)
        return cap({"items": items})

    def milestones(self, since_seq=None):
        rows = self.conn.execute(
            "SELECT * FROM milestones WHERE target_seq > ? ORDER BY target_seq, tier, type", (int(since_seq or 0),)).fetchall()
        state = {1: "confirmed", 2: "automatic", 0: "pending", -1: "rejected"}
        items = [{"seq": r["target_seq"], "hash": self._hash(r["target_seq"]), "type": r["type"], "tier": r["tier"],
                  "score": r["score"], "confirmation": state.get(r["confirmed"], "pending"),
                  "evidence_seq": r["evidence_seq"],
                  "evidence_hash": self._hash(r["evidence_seq"]) if r["evidence_seq"] else None} for r in rows]
        return cap({"items": items})


TOOLS = ("chain_status", "search", "get_node", "lineage", "open_ideas", "discarded", "milestones")
