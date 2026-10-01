#!/usr/bin/env python3
"""Deterministic first drafts for the attorney, built from the index.

    drafts.py disclosure [--project DIR] [--claim SEQ] [--out FILE]
    drafts.py contribution --claim SEQ [--project DIR] [--out FILE]

Writes to <project>/authorship-exports/ (outside .authorship/, which Claude
cannot write). Human text is quoted verbatim, never paraphrased. Every
citation has the form #<seq> (<hash12>) and must pass tests/validate_citations.py.
The disclosure and inventorship-reviewer skills start from these drafts.
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import index  # noqa: E402
import ledger  # noqa: E402
import queries  # noqa: E402
import rules  # noqa: E402

HEADER = "Draft for attorney review. Not legal advice."


class Ctx(object):
    def __init__(self, store):
        self.store = store
        self.q = queries.Q(store)
        self.conn = self.q.conn
        self.entries = {e["seq"]: e for _, _, e in ledger.read_entries(store) if e}

    def cite(self, node_id):
        seq = rules.node_seq(node_id)
        return "#%s (%s)" % (node_id, self.entries[seq]["hash"][:12])

    def text(self, node_id):
        if rules.is_sub(node_id):
            r = self.conn.execute("SELECT label FROM nodes WHERE node_id=?", (str(node_id),)).fetchone()
            return r["label"] if r else ""
        return rules.entry_text(self.entries[rules.node_seq(node_id)], self.store)

    def quote(self, node_id):
        body = self.text(node_id).strip().replace("\n", "\n> ")
        return "> %s\n>\n> %s" % (body, self.cite(node_id))

    def node(self, node_id):
        return self.conn.execute("SELECT * FROM nodes WHERE node_id=?", (str(node_id),)).fetchone()

    def has_confirmed(self):
        return self.conn.execute("SELECT COUNT(*) FROM edges WHERE source='confirmed'").fetchone()[0] > 0

    def has_curated(self):
        """True when the human or the automatic classifier has linked elements."""
        return self.conn.execute("SELECT COUNT(*) FROM edges WHERE source IN ('confirmed', 'auto')").fetchone()[0] > 0

    def lineage_mode(self):
        if self.has_curated():
            return {"curated": True}, ("human-confirmed and automatically classified edges"
                                       if self.has_confirmed() else "automatically classified edges")
        return {}, "rule-derived edges (nothing has been classified or confirmed yet)"

    def claims(self):
        return [r["node_id"] for r in self.conn.execute("SELECT node_id FROM nodes WHERE ibis_type='claim' ORDER BY seq")]

    def milestones(self, typ):
        return self.conn.execute("SELECT * FROM milestones WHERE type=? ORDER BY target_seq", (typ,)).fetchall()


def _out_path(store, name, out):
    if out:
        return os.path.abspath(out)
    d = os.path.join(store.project, "authorship-exports")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%s-%s.md" % (datetime.date.today().isoformat(), name))


def _origin(ctx, nid, lineage_edges):
    n = ctx.node(nid)
    if n is None or n["author"] != "human":
        return "AI"
    modified = [e for e in lineage_edges if e["src"] == str(nid) and e["type"] in ("modifies", "refines")
                and (ctx.node(e["dst"]) or {"author": None})["author"] == "ai"]
    return "mixed" if modified else "human"


def disclosure(store, claim=None, out=None):
    ctx = Ctx(store)
    claims = [str(claim)] if claim else ctx.claims()
    mode, mode_text = ctx.lineage_mode()
    L = ["# Invention disclosure: %s" % os.path.basename(store.project), "", HEADER, "",
         "Generated %s from the authorship ledger. Human text is quoted verbatim. Lineage uses %s." % (
             datetime.date.today().isoformat(),
             mode_text), ""]

    # 1. Problem
    L += ["## 1. Problem", ""]
    issues = [r["node_id"] for r in ctx.conn.execute("SELECT node_id FROM nodes WHERE ibis_type='issue' ORDER BY seq")]
    L += [ctx.quote(i) + "\n" for i in issues] or ["No entry is tagged #problem.", ""]

    # 2. Alternatives considered
    L += ["## 2. Alternatives considered", ""]
    alts = ctx.conn.execute("SELECT * FROM nodes WHERE ibis_type='position' ORDER BY seq, node_id").fetchall()
    for a in alts:
        who = "AI" if a["author"] == "ai" else "Human"
        L.append("- %s, %s, status %s: %s" % (ctx.cite(a["node_id"]), who, a["status"] or "open", rules.short(ctx.text(a["node_id"]), 300)))
    if not alts:
        L.append("None recorded.")
    L.append("")

    # 3. The invention, 4. Element table
    L += ["## 3. The invention", ""]
    table = ["## 4. Elements", "", "| Element | Origin | Citations |", "|---|---|---|"]
    for c in claims:
        L += ["### Claim candidate %s" % ctx.cite(c), "", ctx.quote(c), ""]
        lin = ctx.q.lineage(c, depth=20, **mode)
        edges = lin["edges"]
        rests = [n["node"] for n in lin["items"] if n["author"] == "human" and n["node"] != c
                 and (ctx.node(n["node"]) or {"ibis_type": None})["ibis_type"] in ("position", "decision", "issue", "claim")]
        if rests:
            L += ["It rests on these human entries, quoted verbatim:", ""]
            L += [ctx.quote(n) + "\n" for n in rests]
        for n in lin["items"]:
            nid = n["node"]
            node = ctx.node(nid)
            if node is None or node["ibis_type"] in ("response", "action", "argument"):
                continue
            origin = _origin(ctx, nid, edges)
            cites = [ctx.cite(nid)] + [ctx.cite(e["dst"]) for e in edges if e["src"] == nid and e["type"] in ("modifies", "refines")
                                       and (ctx.node(e["dst"]) or {"author": None})["author"] == "ai"]
            table.append("| %s | %s | %s |" % (" ".join(ctx.text(nid).split()).replace("|", "\\|"), origin, ", ".join(cites)))
        for a in lin["summary"]["ai_elements"]:
            mods = ", ".join(ctx.cite(m) for m in a["modified_by_human_at"])
            L.append("- AI-originated element %s: %s%s" % (ctx.cite(a["node"]), rules.short(a["text"], 200).rstrip("."),
                                                           "; modified by the human at %s" % mods if mods else ""))
        if lin["summary"]["ai_elements"]:
            L.append("")
    if not claims:
        L += ["No entry is tagged #claim.", ""]
    L += table + [""]

    # 5. Reduction to practice
    L += ["## 5. Reduction-to-practice evidence", ""]
    rtp = ctx.milestones("reduction_to_practice")
    for m in rtp:
        L.append("- %s: tests pass at %s after failing, on files edited since the idea." % (
            ctx.cite(m["target_seq"]), ctx.cite(m["evidence_seq"])))
    if not rtp:
        L.append("None recorded.")
    L.append("")

    # 6. Discarded approaches
    L += ["## 6. Discarded approaches", ""]
    for d in ctx.q.discarded()["items"]:
        reason = d.get("reason") or {}
        by = " (reason: %s)" % ctx.cite(reason["node"]) if reason.get("node") and reason["node"] != d["node"] else ""
        L.append("- %s: %s%s" % (ctx.cite(d["node"]), rules.short(ctx.text(d["node"]), 300), by))
    if not ctx.q.discarded()["items"]:
        L.append("None recorded.")
    L.append("")

    # 7. Timeline
    L += ["## 7. Timeline", ""]
    for m in ctx.conn.execute("SELECT * FROM milestones WHERE type != 'session_boundary' ORDER BY target_seq, type"):
        e = ctx.entries[m["target_seq"]]
        L.append("- %s %s: %s%s" % (e["ts"], ctx.cite(m["target_seq"]), m["type"].replace("_", " "),
                                    {1: "", 2: " (automatic)"}.get(m["confirmed"], " (not confirmed)")))
    L.append("")

    # 8. Chain
    c = ctx.q.chain_status()
    L += ["## 8. Chain and anchor status", "",
          "- %d entries; head %s" % (c["entries"], ctx.cite(c["entries"]) if c["entries"] else "-"),
          "- Integrity: %s" % ("verified" if c["ok"] else "BROKEN at #%s: %s" % (c["broken_at"], c["reason"])),
          "- Sealed by external anchors up to %s; %d entries unsealed" % (
              ctx.cite(c["sealed_upto"]) if c["sealed_upto"] else "nothing", c["unsealed"]), ""]
    path = _out_path(store, "disclosure", out)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return path


def contribution(store, claim, out=None):
    """Contribution analysis for one claim: unfavorable facts first."""
    ctx = Ctx(store)
    claim = str(claim)
    mode, _ = ctx.lineage_mode()
    lin = ctx.q.lineage(claim, depth=20, **mode)
    ids = [n["node"] for n in lin["items"]]
    L = ["# Contribution analysis for claim %s" % ctx.cite(claim), "", HEADER, "", ctx.quote(claim), ""]

    L += ["## 1. Unfavorable facts", ""]
    unf = []
    for a in lin["summary"]["ai_elements"]:
        mods = ", ".join(ctx.cite(m) for m in a["modified_by_human_at"])
        unf.append("- AI-originated element %s: %s.%s" % (ctx.cite(a["node"]), rules.short(a["text"], 200).rstrip("."),
                                                          " The human modified it at %s." % mods if mods else " The human did not modify it."))
    for r in ctx.conn.execute("SELECT * FROM annotations WHERE superseded_by IS NULL AND model != ? ORDER BY target_seq",
                              (rules.RULES_VERSION,)):
        st = (json.loads(r["answers_json"] or "{}").get("stance") or {})
        if st.get("choice") == "accepts" and str(r["target_seq"]) in ids:
            p = (st.get("probabilities") or {}).get("accepts")
            unf.append("- %s is classified as accepting an earlier element (p = %.2f, model opinion, not evidence)." % (
                ctx.cite(r["target_seq"]), p or 0))
    for m in ctx.milestones("ai_origin_element"):
        if m["confirmed"] != -1:
            unf.append("- %s is flagged as an AI-origin element (tier %d%s)." % (
                ctx.cite(m["target_seq"]), m["tier"], {1: ", confirmed", 2: ", automatic"}.get(m["confirmed"], ", not confirmed")))
    L += unf or ["None found in the recorded lineage."]
    L.append("")

    L += ["## 2. Human-originated elements", ""]
    hum = [n for n in lin["items"] if n["author"] == "human"]
    for n in hum:
        L += [ctx.quote(n["node"]), ""]
    if not hum:
        L += ["None.", ""]

    L += ["## 3. Gaps where the evidence is thin", ""]
    gaps = []
    if not mode:
        gaps.append("- No element is linked yet (by the classifier or by the human); the lineage above is rule-derived.")
    elif not ctx.has_confirmed():
        gaps.append("- The lineage above comes from the automatic classifier; no link is confirmed by the human.")
    rtp_targets = {m["target_seq"] for m in ctx.milestones("reduction_to_practice")}
    for n in hum:
        if n["seq"] not in rtp_targets and (ctx.node(n["node"]) or {"ibis_type": None})["ibis_type"] == "position":
            gaps.append("- %s has no reduction-to-practice evidence." % ctx.cite(n["node"]))
    for m in ctx.conn.execute("SELECT * FROM milestones WHERE confirmed = 0 AND tier >= 1 ORDER BY target_seq"):
        gaps.append("- %s: %s is a machine suggestion awaiting human confirmation." % (ctx.cite(m["target_seq"]), m["type"]))
    c = ctx.q.chain_status()
    if c["unsealed"]:
        gaps.append("- %d entries are not yet sealed by an external timestamp (sealed up to %s)." % (
            c["unsealed"], ctx.cite(c["sealed_upto"]) if c["sealed_upto"] else "nothing"))
    L += gaps or ["None identified."]
    L.append("")
    path = _out_path(store, "contribution-%s" % claim.replace(".", "_"), out)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return path


def main(argv):
    args = list(argv)

    def flag(name):
        if name in args:
            i = args.index(name)
            v = args[i + 1]
            del args[i:i + 2]
            return v
        return None

    project = flag("--project") or ledger.project_dir()
    claim, out = flag("--claim"), flag("--out")
    store = ledger.Store(project)
    if not store.exists():
        sys.stderr.write("no .authorship/ in %s\n" % project)
        return 1
    cmd = args[0] if args else "disclosure"
    if cmd == "disclosure":
        print(disclosure(store, claim, out))
        return 0
    if cmd == "contribution":
        if not claim:
            sys.stderr.write("contribution needs --claim SEQ\n")
            return 2
        print(contribution(store, claim, out))
        return 0
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
