"""Turns the automatic classifier's answers into annotations: tags, stages,
milestones and lineage edges, with no action from the human.

Thresholds stay asymmetric. A label that favors the human (conception, a
decision, a claim) counts automatically from 0.80; between 0.50 and 0.80 it
waits in `authorship review`. A label against the human (an AI-origin element)
counts automatically from 0.50. The human can still reject or edit any of them.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import classifier as clf  # noqa: E402
import index  # noqa: E402
import ledger  # noqa: E402
import rules  # noqa: E402

FAVORABLE_AUTO = 0.80
REVIEW_LOW = 0.50
UNFAVORABLE_AUTO = 0.50
TAG_MIN = 0.70
EDGE_MIN = 0.50
KIND_TAG = {"problem": "#problem", "idea": "#idea", "hypothesis": "#hypothesis", "decision": "#decision",
            "claim": "#claim", "discard": "#discard"}
KIND_MILESTONE = {"problem": "problem_fixed", "idea": "conception_candidate", "decision": "decision_with_reason",
                  "claim": "claim_candidate", "discard": "discard_with_reason"}
STANCE_EDGE = {"modifies": "modifies", "extends": "refines", "rejects": "rejects", "accepts": "derived_from"}
HUMAN_EVENTS = ("UserPromptSubmit", "ManualNote")
AI_EVENTS = ("Stop", "SubagentStop")


def eligible(e):
    if e.get("actor") == "human" and e.get("event") in HUMAN_EVENTS and not e.get("consent"):
        return True
    return e.get("event") in AI_EVENTS and (e.get("n_blocks") or 0) > 0


def _milestone(typ, score, favorable):
    if favorable:
        if score >= FAVORABLE_AUTO:
            return {"type": typ, "tier": 1, "score": round(score, 4), "band": "high", "auto": True}
        if score >= REVIEW_LOW:
            return {"type": typ, "tier": 1, "score": round(score, 4), "band": "review", "auto": False}
        return None
    if score >= UNFAVORABLE_AUTO:
        return {"type": typ, "tier": 1, "score": round(score, 4), "band": "high" if score >= 0.8 else "review", "auto": True}
    return None


def derive(item, entry, conn, candidates, current_stage=None, builds_on_min=EDGE_MIN):
    """(auto_tags, stage, milestones, edges) for one classified entry. A stage
    counts only when it differs from the stage the work is already in."""
    human = entry.get("actor") == "human"
    tags, milestones, edges, stage = [], [], [], None
    if human:
        kind, kc = item.get("kind"), item.get("kind_confidence", 0.0)
        if kind in KIND_TAG and kc >= TAG_MIN and not entry.get("tags"):
            tags.append(KIND_TAG[kind])
        if kind in KIND_MILESTONE and kind != "idea":
            m = _milestone(KIND_MILESTONE[kind], kc, favorable=True)
            if m:
                milestones.append(m)
        # Conception (spec rule): the human brings a new technical element, originating it or reshaping an
        # earlier one, whatever the entry is otherwise called (an idea, a decision, a claim).
        if kind in ("idea", "hypothesis", "decision", "claim"):
            stance_ok = item.get("stance") in ("originates", "modifies", "rejects") or kind == "idea"
            score = min(item.get("new_element", 0.0), item.get("stance_confidence", 0.0) if kind != "idea" else kc)
            m = _milestone("conception_candidate", score, favorable=True) if stance_ok else None
            if m:
                milestones.append(m)
        target, sc, tc = item.get("target"), item.get("stance_confidence", 0.0), item.get("target_confidence", 0.0)
        if target in candidates and item.get("stance") in STANCE_EDGE:
            p = min(sc, tc)
            if p >= EDGE_MIN:
                edges.append({"src": str(entry["seq"]), "dst": target, "type": STANCE_EDGE[item["stance"]], "p": round(p, 4)})
            t = conn.execute("SELECT author, maturity FROM nodes WHERE node_id=?", (target,)).fetchone()
            if item["stance"] == "accepts" and t and t["author"] == "ai":
                m = _milestone("ai_origin_element", p, favorable=False)
                if m:
                    milestones.append(m)
            if t and t["author"] == "human" and t["maturity"] in clf.LEVELS and item.get("maturity") in clf.LEVELS \
                    and clf.LEVELS.index(item["maturity"]) > clf.LEVELS.index(t["maturity"]):
                m = _milestone("maturity_jump", item.get("maturity_confidence", 0.0), favorable=True)
                if m:
                    milestones.append(m)
        for b in item.get("builds_on") or []:
            if b["id"] in candidates and b["confidence"] >= builds_on_min and not any(x["dst"] == b["id"] for x in edges):
                edges.append({"src": str(entry["seq"]), "dst": b["id"], "type": "derived_from", "p": b["confidence"]})
        new = item.get("new_stage")
        if new and not entry.get("stage") and new.lower() != (current_stage or "").lower():
            stage = item["new_stage"]
            milestones.append({"type": "stage_opened", "tier": 1, "score": round(kc, 4), "band": "high", "auto": True,
                               "detail": stage})
    else:
        if item.get("ai_act") == "unprompted_mechanism":
            m = _milestone("ai_origin_element", item.get("ai_act_confidence", 0.0), favorable=False)
            if m:
                milestones.append(m)
    return tags, stage, milestones, edges


def _candidates(conn, before_seq):
    rows = conn.execute(
        "SELECT node_id, author, label FROM nodes WHERE seq < ? AND ibis_type IN"
        " ('issue','position','decision','claim','remark') ORDER BY seq DESC, node_id LIMIT 60", (before_seq,)).fetchall()
    return [{"id": r["node_id"], "who": "human" if r["author"] == "human" else "AI",
             "text": rules.short(r["label"], 220)} for r in reversed(rows)]


def _payload(store, batch, by_seq, conn):
    first = batch[0]["seq"]
    context = []
    for s in range(first - 1, 0, -1):
        e = by_seq.get(s)
        if e and eligible(e):
            context.append({"seq": s, "who": "human" if e.get("actor") == "human" else "AI",
                            "text": rules.short(rules.entry_text(e, store), 500)})
        if len(context) >= 8:
            break
    entries = [{"seq": e["seq"], "who": "human" if e.get("actor") == "human" else "AI", "event": e["event"],
                "text": rules.entry_text(e, store)[:4000]} for e in batch]
    r = conn.execute("SELECT stage FROM entries WHERE seq < ? AND stage IS NOT NULL ORDER BY seq DESC LIMIT 1",
                     (first,)).fetchone()
    return {"current_stage": r["stage"] if r else "", "context": list(reversed(context)),
            "candidates": _candidates(conn, batch[-1]["seq"]), "entries": entries}


def run(store, entries, anns, classifier=None, limit=60):
    """Classify every eligible entry that has no automatic annotation yet.
    Returns (annotations written, state)."""
    import annotator

    if classifier is None:
        classifier = clf.default_backend()
        if classifier is None:
            return 0, "no-backend"
    done = annotator.latest_by_target(anns, method="auto")
    pending = [e for e in entries if eligible(e) and e["seq"] not in done][:limit]
    if not pending:
        return 0, "on"
    by_seq = {e["seq"]: e for e in entries}
    conn = index.update(store)
    written = 0
    try:
        for i in range(0, len(pending), clf.BATCH):
            batch = pending[i:i + clf.BATCH]
            payload = _payload(store, batch, by_seq, conn)
            answers, model, _cost = classifier.classify(payload)
            got = {}
            for a in answers:
                try:
                    got[int(a.get("seq"))] = clf.normalize(a)
                except (TypeError, ValueError):
                    continue
            cand_ids = {c["id"] for c in payload["candidates"]}
            stage = payload["current_stage"]
            for e in batch:
                item = got.get(e["seq"])
                if item is None:
                    continue
                tags, new_stage, milestones, edges = derive(item, e, conn, cand_ids, stage,
                                                            getattr(classifier, "builds_on_min", EDGE_MIN))
                stage = new_stage or e.get("stage") or stage
                annotator.write_annotation(store, {
                    "id": "ann_" + uuid.uuid4().hex, "ts": ledger.now_iso(), "target_seq": e["seq"],
                    "target_hash": e["hash"], "method": "auto", "backend": classifier.name, "model": model,
                    "questions_hash": getattr(classifier, "prompt_hash", clf.PROMPT_HASH),
                    "answers": item, "milestones": milestones, "edges": edges, "auto_tags": tags, "stage": new_stage,
                    "supersedes": None, "context": {"candidates": sorted(cand_ids, key=lambda x: (rules.node_seq(x), x))}})
                written += 1
            conn.close()
            conn = index.update(store)  # later batches see the tags and stages just written
    finally:
        conn.close()
    return written, "on"
