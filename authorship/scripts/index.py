#!/usr/bin/env python3
"""Query index over the ledger: .authorship/index.sqlite (derived, gitignored).

    index.py [--project DIR]            incremental update
    index.py --rebuild [--project DIR]  drop and rebuild from the ledger
    index.py review-queue [--project DIR] [--json]

Entries, nodes and rule edges are indexed incrementally from the last indexed
seq; the rule engine's state is persisted, so an incremental run produces the
same rows as a rebuild. Tables that depend on confirmations and annotations
are recomputed in full on every update (cheap, deterministic).
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger  # noqa: E402
import rules  # noqa: E402

SCHEMA_VERSION = "index-v2"
LINEAGE_TYPES = ("derived_from", "modifies", "refines", "responds_to")
AUTO = 2  # milestones.confirmed: 1 confirmed by the human, 2 automatic, 0 pending, -1 rejected
EDGE_TYPES = ("responds_to", "refines", "modifies", "implements", "supports", "objects_to", "rejects",
              "supersedes", "discards", "derived_from")
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS entries(seq INTEGER PRIMARY KEY, ts TEXT, event TEXT, actor TEXT, session TEXT,
  kind TEXT, tool TEXT, outcome TEXT, file TEXT, text TEXT, tags_json TEXT, hash TEXT, stage TEXT,
  stage_tag TEXT, auto_tags_json TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS nodes(node_id TEXT PRIMARY KEY, seq INTEGER, option INTEGER, ibis_type TEXT,
  stage TEXT, author TEXT, status TEXT, maturity TEXT, label TEXT);
CREATE INDEX IF NOT EXISTS nodes_seq ON nodes(seq);
CREATE TABLE IF NOT EXISTS edges(src TEXT, dst TEXT, type TEXT, source TEXT, evidence_seq INTEGER,
  UNIQUE(src, dst, type, source));
CREATE INDEX IF NOT EXISTS edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS confirmations(seq INTEGER PRIMARY KEY, target_seq INTEGER, target_hash TEXT,
  annotation_id TEXT, decision TEXT, label TEXT, edited_label TEXT);
CREATE TABLE IF NOT EXISTS annotations(id TEXT PRIMARY KEY, target_seq INTEGER, model TEXT, questions_hash TEXT,
  answers_json TEXT, milestones_json TEXT, edges_json TEXT, supersedes TEXT, superseded_by TEXT, ts TEXT,
  method TEXT, auto_tags_json TEXT, stage TEXT);
CREATE TABLE IF NOT EXISTS milestones(target_seq INTEGER, type TEXT, tier INTEGER, score REAL, confirmed INTEGER,
  evidence_seq INTEGER, annotation_id TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, tokenize='unicode61 remove_diacritics 2');
"""


def connect(store, path=None):
    conn = sqlite3.connect(path or store.index, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _meta(conn, key, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def _set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, json.dumps(value)))


# ---------------------------------------------------------------------------
# Incremental rule engine


class Engine(object):
    """Sequential pass over entries. All state lives in self.s (JSON)."""

    def __init__(self, conn, store, state):
        self.conn, self.store = conn, store
        self.s = state or {"stage": None, "last_prompt": None, "last_human": None, "last_ai": None,
                           "last_issue": None}

    def node(self, node_id, e, ibis, author, label, option=None, stage=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO nodes(node_id, seq, option, ibis_type, stage, author, status, maturity, label)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (str(node_id), e["seq"], option, ibis, stage, author, None, None, rules.short(label, 240)))

    def edge(self, src, dst, typ, evidence=None):
        if src is None or dst is None or str(src) == str(dst):
            return
        self.conn.execute("INSERT OR IGNORE INTO edges(src, dst, type, source, evidence_seq) VALUES(?,?,?,?,?)",
                          (str(src), str(dst), typ, "rule", evidence))

    def process(self, e):
        s, seq, ev, actor = self.s, e["seq"], e.get("event"), e.get("actor")
        tags = e.get("tags") or []
        if e.get("stage"):
            s["stage"] = e["stage"]
        stage = s["stage"] if actor != "system" and ev != "Confirm" else None
        text = rules.entry_text(e, self.store)
        self.conn.execute(
            "INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'[]')",
            (seq, e.get("ts"), ev, actor, e.get("session"), e.get("kind"), e.get("tool"), e.get("outcome"),
             e.get("file"), text, json.dumps(tags), e.get("hash"), stage, e.get("stage")))
        searchable = " ".join(x for x in (text, e.get("command"), e.get("file"), e.get("error"), " ".join(tags)) if x)
        self.conn.execute("INSERT OR REPLACE INTO fts(rowid, text) VALUES(?, ?)", (seq, searchable))

        if actor == "human" and ev in ("UserPromptSubmit", "ManualNote"):
            ibis = rules.human_ibis(tags)
            self.node(seq, e, ibis, "human", text, stage=stage)
            if ev == "UserPromptSubmit":
                self.edge(seq, s["last_ai"], "responds_to", seq)
                s["last_prompt"] = seq
            if ibis == "issue":
                s["last_issue"] = seq
            s["last_human"] = seq
        elif ev in ("Stop", "SubagentStop"):
            self.node(seq, e, "response", "ai", text, stage=stage)
            self.edge(seq, s["last_prompt"], "responds_to", seq)
            for n, label in rules.parse_options(text):
                nid = "%d.%d" % (seq, n)
                self.node(nid, e, "position", "ai", label, option=n, stage=stage)
                self.edge(nid, s["last_prompt"], "responds_to", seq)
            for i, label in enumerate(rules.parse_ai_proposals(text), 1):
                nid = "%d.p%d" % (seq, i)
                self.node(nid, e, "position", "ai", "AI proposal: " + label, stage=stage)
                self.edge(nid, s["last_prompt"], "responds_to", seq)
            s["last_ai"] = seq
        elif e.get("kind") == "tool":
            result = rules.test_result(e, self.store)
            if result:
                label = "%s: %s" % ("tests fail" if result == "fail" else "tests pass", e.get("command"))
                self.node(seq, e, "argument", "ai", label, stage=stage)
                self.edge(seq, s["last_prompt"], "objects_to" if result == "fail" else "supports", seq)
            else:
                label = "%s %s" % (e.get("tool"), e.get("file") or e.get("command") or "")
                self.node(seq, e, "action", "ai", label, stage=stage)
                if e.get("tool") in rules.EDIT_TOOLS and e.get("outcome") == "success":
                    self.edge(seq, s["last_prompt"], "implements", seq)
        elif ev == "Confirm":
            self.conn.execute("INSERT OR REPLACE INTO confirmations VALUES(?,?,?,?,?,?,?)",
                              (seq, e.get("target_seq"), e.get("target_hash"), e.get("annotation_id"),
                               e.get("decision"), e.get("label"), e.get("edited_label")))


# ---------------------------------------------------------------------------
# Derived tables (recomputed in full)


def parse_edge_label(label):
    """'edge:<src>:<type>:<dst>' -> (src, type, dst) or None."""
    if not label or not label.startswith("edge:"):
        return None
    parts = label.split(":")
    if len(parts) != 4 or parts[2] not in EDGE_TYPES:
        return None
    return parts[1], parts[2], parts[3]


def effective_confirmations(conn):
    """Latest decision per (target_seq, label). Returns {(target, label): (decision, final_label, seq)}."""
    out = {}
    for r in conn.execute("SELECT * FROM confirmations ORDER BY seq"):
        final = r["edited_label"] if r["decision"] == "edit" else r["label"]
        out[(r["target_seq"], r["label"])] = (r["decision"], final, r["seq"])
    return out


def _load_annotations(conn, store):
    off = _meta(conn, "annotations_offset", 0)
    if not os.path.exists(store.annotations):
        return
    size = os.path.getsize(store.annotations)
    if size < off:
        conn.execute("DELETE FROM annotations")
        off = 0
    with open(store.annotations, "rb") as f:
        f.seek(off)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        return
    for raw in data[: end + 1].split(b"\n"):
        if not raw.strip():
            continue
        try:
            a = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO annotations(id, target_seq, model, questions_hash, answers_json, milestones_json,"
            " edges_json, supersedes, superseded_by, ts, method, auto_tags_json, stage) VALUES(?,?,?,?,?,?,?,?,NULL,?,?,?,?)",
            (a.get("id"), a.get("target_seq"), a.get("model"), a.get("questions_hash"),
             json.dumps(a.get("answers") or {}), json.dumps(a.get("milestones") or []),
             json.dumps(a.get("edges") or []), a.get("supersedes"), a.get("ts"),
             a.get("method") or ("rules" if a.get("model") == rules.RULES_VERSION else "jev"),
             json.dumps(a.get("auto_tags") or []), a.get("stage")))
    _set_meta(conn, "annotations_offset", off + end + 1)


def recompute(conn, store):
    _load_annotations(conn, store)
    conn.execute("UPDATE annotations SET superseded_by=NULL")
    conn.execute("UPDATE annotations SET superseded_by=(SELECT b.id FROM annotations b WHERE b.supersedes=annotations.id"
                 " ORDER BY b.ts, b.id LIMIT 1)")
    conf = effective_confirmations(conn)

    # confirmed edges
    conn.execute("DELETE FROM edges WHERE source IN ('confirmed', 'annotation', 'auto')")
    rejected = set()
    for (target, label), (decision, final, seq) in sorted(conf.items(), key=lambda kv: kv[1][2]):
        orig = parse_edge_label(label)
        if decision == "reject":
            if orig:
                rejected.add(orig)
            continue
        parsed = parse_edge_label(final)
        if decision == "edit" and orig:
            rejected.add(orig)
        if parsed:
            conn.execute("INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?)", (parsed[0], parsed[2], parsed[1], "confirmed", seq))
    # annotation edges (suggestions) from active annotations, minus rejected ones
    # Machine edges, minus the ones the human rejected. The automatic classifier's
    # edges count as they are ("auto"); Jev's wait for confirmation ("annotation").
    for r in conn.execute("SELECT id, edges_json, method FROM annotations WHERE superseded_by IS NULL ORDER BY id").fetchall():
        source = "auto" if r["method"] == "auto" else "annotation"
        for ed in json.loads(r["edges_json"] or "[]"):
            key = (str(ed.get("src")), ed.get("type"), str(ed.get("dst")))
            if key in rejected or key[1] not in EDGE_TYPES:
                continue
            conn.execute("INSERT OR IGNORE INTO edges VALUES(?,?,?,?,NULL)", (key[0], key[2], key[1], source))

    # milestones
    conn.execute("DELETE FROM milestones")
    for r in conn.execute("SELECT id, target_seq, milestones_json FROM annotations WHERE superseded_by IS NULL"
                          " ORDER BY target_seq, id").fetchall():
        for m in json.loads(r["milestones_json"] or "[]"):
            typ, tier = m.get("type"), int(m.get("tier", 0))
            label = "milestone:%s" % typ
            decision = conf.get((r["target_seq"], label))
            if decision and decision[0] == "reject":
                confirmed = -1
            elif decision and decision[0] in ("accept", "edit"):
                confirmed = 1
                if decision[0] == "edit" and decision[1].startswith("milestone:"):
                    typ = decision[1].split(":", 1)[1]
            elif tier == 0 and (typ in rules.HUMAN_DECLARED or typ in rules.FACTUAL):
                confirmed = 1  # declared by the human through a tag, or a structural fact
            elif m.get("auto"):
                confirmed = AUTO  # counted automatically; the human may still reject or edit it
            else:
                confirmed = 0
            conn.execute("INSERT INTO milestones VALUES(?,?,?,?,?,?,?)",
                         (r["target_seq"], typ, tier, m.get("score"), confirmed, m.get("evidence_seq"), r["id"]))

    apply_auto(conn)

    # node status and maturity
    conn.execute("UPDATE nodes SET status=NULL, maturity=NULL")
    conn.execute("UPDATE nodes SET status='open' WHERE ibis_type IN ('position','claim','decision','issue','remark')")
    conn.execute("UPDATE nodes SET status='adopted' WHERE node_id IN (SELECT dst FROM edges WHERE type IN"
                 " ('implements','supports','refines','derived_from') AND source != 'annotation')")
    conn.execute("UPDATE nodes SET status='modified' WHERE node_id IN (SELECT dst FROM edges WHERE type='modifies'"
                 " AND source != 'annotation')")
    conn.execute("UPDATE nodes SET status='rejected' WHERE node_id IN (SELECT dst FROM edges WHERE type='rejects'"
                 " AND source != 'annotation')")
    conn.execute("UPDATE nodes SET status='superseded' WHERE node_id IN (SELECT dst FROM edges WHERE type='supersedes'"
                 " AND source != 'annotation')")
    conn.execute("UPDATE nodes SET status='discarded' WHERE node_id IN (SELECT dst FROM edges WHERE type='discards'"
                 " AND source != 'annotation') OR seq IN (SELECT seq FROM entries WHERE tags_json LIKE '%\"#discard\"%' OR auto_tags_json LIKE '%\"#discard\"%')"
                 " AND option IS NULL AND node_id NOT LIKE '%.p%'")
    for r in conn.execute("SELECT id, target_seq, answers_json FROM annotations WHERE superseded_by IS NULL"
                          " ORDER BY ts, id").fetchall():
        ans = json.loads(r["answers_json"] or "{}")
        mat = ans.get("maturity")
        if isinstance(mat, dict):  # Jev: {"choice": ...}
            mat = mat.get("choice")
        elif ans.get("kind") not in ("idea", "hypothesis", "decision", "claim") or ans.get("maturity_confidence", 0) < 0.5:
            mat = None  # automatic classifier: maturity only means something for ideas
        if mat:
            conn.execute("UPDATE nodes SET maturity=? WHERE node_id=?", (mat, str(r["target_seq"])))
    for (target, label), (decision, final, _) in conf.items():
        if decision in ("accept", "edit") and final and final.startswith("maturity:"):
            conn.execute("UPDATE nodes SET maturity=? WHERE node_id=?", (final.split(":", 1)[1], str(target)))


def apply_auto(conn):
    """Automatic tags and stages from the classifier. Human tags and #stage always win."""
    conn.execute("UPDATE entries SET auto_tags_json='[]'")
    auto_stage = {}
    for r in conn.execute("SELECT target_seq, auto_tags_json, stage FROM annotations WHERE method='auto'"
                          " AND superseded_by IS NULL ORDER BY ts, id").fetchall():
        conn.execute("UPDATE entries SET auto_tags_json=? WHERE seq=? AND tags_json='[]'",
                     (r["auto_tags_json"] or "[]", r["target_seq"]))
        if r["stage"]:
            auto_stage[r["target_seq"]] = r["stage"]
    current = None
    for r in conn.execute("SELECT seq, event, actor, stage, stage_tag FROM entries ORDER BY seq").fetchall():
        if r["stage_tag"]:
            current = r["stage_tag"]
        elif r["seq"] in auto_stage:
            current = auto_stage[r["seq"]]
        stage = current if r["actor"] != "system" and r["event"] != "Confirm" else None
        if stage != r["stage"]:
            conn.execute("UPDATE entries SET stage=? WHERE seq=?", (stage, r["seq"]))
    conn.execute("UPDATE nodes SET stage=(SELECT stage FROM entries WHERE entries.seq=nodes.seq)")
    for r in conn.execute("SELECT n.node_id, e.auto_tags_json FROM nodes n JOIN entries e ON e.seq=n.seq"
                          " WHERE n.author='human' AND n.option IS NULL AND e.tags_json='[]'").fetchall():
        conn.execute("UPDATE nodes SET ibis_type=? WHERE node_id=?",
                     (rules.human_ibis(json.loads(r["auto_tags_json"] or "[]")), r["node_id"]))


def update(store, conn=None, rebuild=False):
    """Bring the index up to date. Returns the connection."""
    if rebuild:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(store.index + suffix)
            except OSError:
                pass
    conn = conn or connect(store)
    if _meta(conn, "schema") not in (None, SCHEMA_VERSION):
        conn.close()
        return update(store, rebuild=True)
    last = _meta(conn, "last_seq", 0)
    head = _meta(conn, "last_hash")
    engine = Engine(conn, store, _meta(conn, "engine"))
    with conn:
        _set_meta(conn, "schema", SCHEMA_VERSION)
        for _, _, e in ledger.read_entries(store):
            if not e or e.get("seq", 0) <= last:
                if e and e.get("seq") == last and head and e.get("hash") != head:
                    # ledger replaced under us: start over
                    conn.close()
                    return update(store, rebuild=True)
                continue
            engine.process(e)
            last, head = e["seq"], e["hash"]
        _set_meta(conn, "last_seq", last)
        _set_meta(conn, "last_hash", head)
        _set_meta(conn, "engine", engine.s)
        recompute(conn, store)
    return conn


def store_status(conn, chain, review_count):
    """Cache chain status and queue size for the status line (which reads only head.json and this)."""
    with conn:
        _set_meta(conn, "chain", {k: chain.get(k) for k in ("ok", "entries", "head", "broken_at", "sealed_upto", "unsealed")})
        _set_meta(conn, "review_count", review_count)


# ---------------------------------------------------------------------------
# Queries used by the MCP server, viewer, skills and status line


def lineage(conn, node_id, depth=10, confirmed_only=False, curated=False):
    """Ancestors of node_id through lineage edges. Returns {node_id: depth}.
    confirmed_only: human-confirmed edges. curated: confirmed plus automatic
    (classifier) edges, leaving out the rule that links every prompt to the
    reply before it, and Jev suggestions still waiting for confirmation."""
    where = "AND e.source = 'confirmed'" if confirmed_only else (
        "AND e.source IN ('confirmed', 'auto')" if curated else "")
    q = ("WITH RECURSIVE anc(node, depth) AS (SELECT ?, 0 UNION"
         " SELECT e.dst, anc.depth + 1 FROM edges e JOIN anc ON e.src = anc.node"
         " WHERE e.type IN (%s) AND anc.depth < ? %s)"
         " SELECT node, MIN(depth) AS d FROM anc GROUP BY node") % (
        ",".join("'%s'" % t for t in LINEAGE_TYPES), where)
    return {r["node"]: r["d"] for r in conn.execute(q, (str(node_id), depth))}


def review_queue(conn, low=0.50, high=0.80, include_auto=False):
    """Machine suggestions the human has not decided. With include_auto, also the
    automatic labels and links already counted, so the human can correct them."""
    conf = effective_confirmations(conn)
    out = []
    states = "(0, %d)" % AUTO if include_auto else "(0)"
    for r in conn.execute("SELECT m.*, n.label FROM milestones m LEFT JOIN nodes n ON n.node_id = CAST(m.target_seq AS TEXT)"
                          " WHERE m.tier >= 1 AND m.confirmed IN %s ORDER BY m.target_seq" % states):
        if r["score"] is None or r["score"] >= low:
            out.append({"kind": "milestone", "target_seq": r["target_seq"], "label": "milestone:%s" % r["type"],
                        "score": r["score"], "annotation_id": r["annotation_id"], "text": r["label"],
                        "automatic": r["confirmed"] == AUTO})
    sources = "('annotation', 'auto')" if include_auto else "('annotation')"
    for r in conn.execute("SELECT e.src, e.dst, e.type, e.source FROM edges e WHERE e.source IN %s ORDER BY e.src, e.dst"
                          % sources):
        label = "edge:%s:%s:%s" % (r["src"], r["type"], r["dst"])
        target = rules.node_seq(r["src"])
        if (target, label) in conf:
            continue
        ann = conn.execute("SELECT id FROM annotations WHERE target_seq=? AND superseded_by IS NULL ORDER BY ts DESC LIMIT 1",
                           (target,)).fetchone()
        out.append({"kind": "edge", "target_seq": target, "label": label, "score": None,
                    "annotation_id": ann["id"] if ann else None, "automatic": r["source"] == "auto"})
    return out


def main(argv):
    args = list(argv)
    project = ledger.project_dir()
    if "--project" in args:
        i = args.index("--project")
        project = os.path.abspath(args[i + 1])
        del args[i:i + 2]
    store = ledger.Store(project)
    if not store.exists():
        sys.stderr.write("no .authorship/ in %s\n" % project)
        return 1
    if args and args[0] == "review-queue":
        conn = update(store)
        q = review_queue(conn)
        if "--json" in args:
            print(json.dumps(q))
        else:
            for item in q:
                print("#%s  %-40s %s" % (item["target_seq"], item["label"], "" if item["score"] is None else "%.2f" % item["score"]))
            print("%d item(s) to review." % len(q))
        return 0
    conn = update(store, rebuild="--rebuild" in args)
    n = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    print("indexed %d entries" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
