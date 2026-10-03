#!/usr/bin/env python3
"""Milestone annotator: tails the ledger and writes machine opinions to
annotations.jsonl. Never writes the ledger (except the human's consent note,
typed by the human in a terminal).

    annotator.py run [--project DIR]          one pass (Tier 0, plus Tier 1 when enabled)
    annotator.py daemon [--project DIR]       keep running; started by SessionStart
    annotator.py consent [--project DIR]      human only: allow Tier 1 (Jev) to send text
    annotator.py calibrate [--project DIR]    human only: label ~100 entries, report precision/recall

Tier 0: deterministic rules, always on, offline.
Tier 1: Jev, opt-in (AUTHORSHIP_JEV=1, an API key, and a recorded consent note).
"""
import hashlib
import json
import math
import os
import re
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import index  # noqa: E402
import ledger  # noqa: E402
import rules  # noqa: E402

QUESTIONS_PATH = os.path.join(ledger.SCRIPTS_DIR, "questions.toml")
TIER0_QHASH = hashlib.sha256(rules.RULES_VERSION.encode()).hexdigest()
FAVORABLE = ("conception_candidate", "maturity_jump")
IDEA_TAGS = ("#idea", "#hypothesis", "#decision")


# ---------------------------------------------------------------------------
# TOML (tomllib when available; a small subset parser otherwise, for 3.9/3.10)


def _toml_value(raw):
    raw = raw.strip()
    if raw.startswith('"'):
        return json.loads(raw[: raw.index('"', 1) + 1])
    if raw.startswith("["):
        inner = raw[1: raw.rindex("]")]
        return [_toml_value(x) for x in re.findall(r'"[^"]*"|[^,\s][^,]*', inner) if x.strip()]
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return raw


def load_toml(path):
    try:
        import tomllib  # Python 3.11+

        with open(path, "rb") as f:
            return tomllib.load(f)
    except ImportError:
        pass
    data, cur = {}, None
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.split("#", 1)[0].strip() if not re.search(r'"[^"]*#', line) else line.strip()
            if not s:
                continue
            m = re.match(r"^\[([^\]]+)\]$", s)
            if m:
                cur = data
                for part in m.group(1).split("."):
                    cur = cur.setdefault(part.strip(), {})
                continue
            k, _, v = s.partition("=")
            v = v.strip()
            if v.startswith('"') and "#" in v:
                v = v[: v.index('"', 1) + 1]
            (cur if cur is not None else data)[k.strip()] = _toml_value(v)
    return data


def load_questions(path=QUESTIONS_PATH):
    with open(path, "rb") as f:
        qhash = hashlib.sha256(f.read()).hexdigest()
    cfg = load_toml(path)
    cfg["_hash"] = qhash
    return cfg


def wire_questions(cfg, event):
    """Jev wire-format questions that apply to an event."""
    human = event in ("UserPromptSubmit", "ManualNote")
    out = {}
    for name, q in cfg.get("questions", {}).items():
        on = q.get("on")
        if (on and event not in on) or (not on and not human):
            continue
        t = q["type"]
        w = {"type": t, "instructions": q.get("ask") or name.replace("_", " ")}
        if t == "choice":
            crit = q.get("criteria") or {}
            w["criteria"] = {o: crit.get(o, o.replace("_", " ")) for o in q["options"]}
        elif t == "score":
            w["criteria"] = list(q["levels"])
        out[name] = w
    return out


# ---------------------------------------------------------------------------
# Rule expressions:  a >= 0.8 and b in [x, y] and p(b) >= 0.7 or c == z and p >= 0.5

_TOK = re.compile(r"\s*(>=|<=|==|!=|>|<|\[|\]|\(|\)|,|[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?)")


def _tokenize(expr):
    pos, out = 0, []
    expr = expr.strip()
    while pos < len(expr):
        m = _TOK.match(expr, pos)
        if not m:
            raise ValueError("bad rule near %r" % expr[pos:])
        out.append(m.group(1))
        pos = m.end()
    return out


def _cmp(a, op, b):
    return {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b, "==": a == b, "!=": a != b}[op]


def eval_rule(expr, answers, variables=None):
    """Returns (matched, score). score is the smallest probability that the best
    satisfied conjunction relied on (1.0 when it relied on none)."""
    variables = variables or {}
    best = None
    for conj in " ".join(_tokenize(expr)).split(" or "):
        toks = conj.split(" ")
        ok, probs, last_choice, i = True, [], None, 0
        terms = []
        cur = []
        for t in toks:
            if t == "and":
                terms.append(cur)
                cur = []
            else:
                cur.append(t)
        terms.append(cur)
        for term in terms:
            if term[:2] == ["p", "("]:
                name, rest = term[2], term[4:]
                a = answers.get(name) or {}
                val = a.get("p") if a.get("type") == "noul" else (a.get("probabilities") or {}).get(a.get("choice"), 0.0)
                val = val or 0.0
                ok = ok and _cmp(val, rest[0], float(rest[1]))
                probs.append(val)
                continue
            name, op, rest = term[0], term[1], term[2:]
            if name == "p":
                a = answers.get(last_choice) or {}
                val = (a.get("probabilities") or {}).get(a.get("choice"), 0.0)
                ok = ok and _cmp(val, op, float(rest[0]))
                probs.append(val)
                continue
            if name in variables:
                ok = ok and _cmp(float(variables[name]), op, float(rest[0]))
                continue
            a = answers.get(name)
            if not a:
                ok = False
                continue
            if op == "in":
                opts = [x for x in rest if x not in ("[", "]", ",")]
                ok = ok and a.get("choice") in opts
                last_choice = name
            elif a.get("type") == "noul":
                ok = ok and _cmp(a["p"], op, float(rest[0]))
                probs.append(a["p"])
            elif op in ("==", "!=") and not re.match(r"^\d", rest[0]):
                ok = ok and _cmp(a.get("choice"), op, rest[0])
                last_choice = name
            else:
                v = a.get("score") if a.get("type") == "score" else (a.get("probabilities") or {}).get(a.get("choice"), 0.0)
                ok = ok and _cmp(float(v or 0.0), op, float(rest[0]))
        if ok:
            score = min(probs) if probs else 1.0
            best = score if best is None else max(best, score)
    return best is not None, best


# ---------------------------------------------------------------------------
# Candidates for "which node" questions: stdlib TF-IDF


def _terms(text):
    return [t for t in re.findall(r"\w+", (text or "").lower()) if len(t) >= 3 and not t.isdigit()]


def tfidf_candidates(target_text, docs, k=20):
    """docs: [(node_id, text)] -> top-k [(node_id, score)] by cosine similarity."""
    if not docs:
        return []
    df = {}
    tokenized = []
    for nid, text in docs:
        ts = _terms(text)
        tokenized.append((nid, ts))
        for t in set(ts):
            df[t] = df.get(t, 0) + 1
    n = len(docs)

    def vec(ts):
        tf = {}
        for t in ts:
            tf[t] = tf.get(t, 0) + 1
        v = {t: c * (math.log((1 + n) / (1 + df.get(t, 0))) + 1) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    q = vec(_terms(target_text))
    scored = []
    for nid, ts in tokenized:
        d = vec(ts)
        scored.append((nid, sum(q.get(t, 0) * x for t, x in d.items())))
    scored.sort(key=lambda x: (-x[1], rules.node_seq(x[0]), x[0]))
    return scored[:k]


# ---------------------------------------------------------------------------
# Annotation file


def read_annotations(store):
    out = []
    if not os.path.exists(store.annotations):
        return out
    with open(store.annotations, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def method_of(a):
    """rules (Tier 0), auto (automatic classifier) or jev (Tier 1, opt-in)."""
    return a.get("method") or ("rules" if a.get("model") == rules.RULES_VERSION else "jev")


def latest_by_target(anns, tier0=None, method=None):
    """{target_seq: annotation} for the newest non-superseded annotation of one method.
    tier0=True means method "rules"; tier0=False means "jev" (kept for older callers)."""
    if method is None:
        method = "rules" if tier0 else "jev"
    superseded = {a.get("supersedes") for a in anns if a.get("supersedes")}
    out = {}
    for a in anns:
        if method_of(a) != method or a["id"] in superseded:
            continue
        out[a["target_seq"]] = a
    return out


def write_annotation(store, ann):
    with open(store.annotations, "a", encoding="utf-8") as f:
        f.write(ledger.canonical_json(ann) + "\n")


def _new_id():
    return "ann_" + uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Tier 0


def tier0_milestones(store, entries, idea_seqs=()):
    """Deterministic milestones over the whole ledger: {target_seq: [milestone]}.
    idea_seqs: entries the automatic classifier found to be ideas, hypotheses or
    decisions; they anchor reduction-to-practice evidence like tagged ones."""
    out = {}

    def add(seq, m):
        out.setdefault(seq, []).append(m)

    idea, edited, red, rtp_done = None, set(), False, set()
    for e in entries:
        seq, ev, actor = e["seq"], e.get("event"), e.get("actor")
        tags = e.get("tags") or []
        if ev in ("SessionStart", "SessionEnd", "SessionReconciled"):
            add(seq, {"type": "session_boundary", "tier": 0, "score": 1.0, "detail": ev})
        if actor == "human" and ev in ("UserPromptSubmit", "ManualNote"):
            for m in rules.tag_milestones(tags):
                d = {"type": m, "tier": 0, "score": 1.0}
                if m == "stage_opened":
                    d["detail"] = e.get("stage")
                add(seq, d)
            if any(t in tags for t in IDEA_TAGS) or seq in idea_seqs:
                idea, edited, red = seq, set(), False
        if ev in ("Stop", "SubagentStop"):
            for label in rules.parse_ai_proposals(rules.entry_text(e, store)):
                add(seq, {"type": "ai_origin_element", "tier": 0, "score": 1.0, "detail": rules.short(label, 200)})
        if e.get("kind") == "tool":
            f = rules.file_of(e)
            if f and e.get("outcome") == "success" and idea is not None:
                edited.add(f)
            result = rules.test_result(e, store)
            if result == "fail":
                red = True
            elif result == "pass":
                if red and edited and idea is not None and idea not in rtp_done:
                    add(idea, {"type": "reduction_to_practice", "tier": 0, "score": 1.0, "evidence_seq": seq,
                               "detail": sorted(edited)})
                    rtp_done.add(idea)
                red = False
    for seq in out:
        out[seq].sort(key=lambda m: (m["type"], m.get("evidence_seq") or 0))
    return out


def auto_idea_seqs(anns):
    out = set()
    for seq, a in latest_by_target(anns, method="auto").items():
        if set(a.get("auto_tags") or []) & set(IDEA_TAGS):
            out.add(seq)
    return out


def run_tier0(store, entries, anns):
    by_seq = {e["seq"]: e for e in entries}
    current = latest_by_target(anns, tier0=True)
    wanted = tier0_milestones(store, entries, auto_idea_seqs(anns))
    written = 0
    for seq in sorted(set(wanted) | set(current)):
        ms = wanted.get(seq, [])
        prev = current.get(seq)
        if prev is not None and prev.get("milestones") == ms:
            continue
        if prev is None and not ms:
            continue
        write_annotation(store, {
            "id": _new_id(), "ts": ledger.now_iso(), "target_seq": seq, "target_hash": by_seq[seq]["hash"],
            "model": rules.RULES_VERSION, "questions_hash": TIER0_QHASH, "answers": {}, "milestones": ms, "edges": [],
            "supersedes": prev["id"] if prev else None})
        written += 1
    return written


# ---------------------------------------------------------------------------
# Tier 1 (Jev)


def jev_enabled():
    return os.environ.get("AUTHORSHIP_JEV") == "1"


def find_consent(entries):
    for e in entries:
        if e.get("event") == "ManualNote" and isinstance(e.get("consent"), dict) and e["consent"].get("scope") == "jev":
            return e
    return None


STANCE_EDGE = {"modifies": "modifies", "extends": "refines", "rejects": "rejects", "accepts": "derived_from"}


def _tier1_one(store, conn, e, entries_by_seq, cfg, provider, prev_ai_text, open_ideas):
    import jev_client

    ev = e["event"]
    questions = wire_questions(cfg, ev)
    text = rules.entry_text(e, store)
    candidates = []
    if ev in ("UserPromptSubmit", "ManualNote"):
        docs = [(r["node_id"], r["label"]) for r in conn.execute(
            "SELECT node_id, label FROM nodes WHERE seq < ? AND ibis_type IN ('issue','position','decision','claim','remark')"
            " ORDER BY seq, node_id", (e["seq"],))]
        candidates = [nid for nid, _ in tfidf_candidates(text, docs, 20)]
        if candidates:
            labels = dict(docs)
            crit = {"n" + c: rules.short(labels[c], 200) for c in candidates}
            crit["none"] = "None of these: the entry does not respond to an earlier element."
            questions["target"] = {"type": "choice", "criteria": crit,
                                   "instructions": "Which earlier element does this entry extend, modify, accept or reject?"}
    if not questions:
        return None
    state = {"entry": text[:16000]}
    if "previous_ai_response" in cfg.get("state", []):
        state["previous_ai_response"] = (prev_ai_text or "")[:8000]
    if "open_ideas" in cfg.get("state", []):
        state["open_ideas"] = open_ideas[:20]
    answers = jev_client.evaluate(state, questions, provider=provider, model=cfg.get("model"))
    reported = answers.pop("_model", None)

    variables = {}
    target = None
    if "target" in answers and answers["target"]["choice"] not in (None, "none"):
        target = answers["target"]["choice"][1:]
    if "maturity" in answers:
        levels = cfg["questions"]["maturity"]["levels"]
        cur = levels.index(answers["maturity"]["choice"]) if answers["maturity"]["choice"] in levels else 0
        prev = -1
        if target:
            r = conn.execute("SELECT author, maturity FROM nodes WHERE node_id=?", (target,)).fetchone()
            if r and r["author"] == "human" and r["maturity"] in levels:
                prev = levels.index(r["maturity"])
        variables["maturity_delta"] = cur - prev

    milestones, edges = [], []
    r = cfg.get("rules", {})
    if r.get("conception_candidate") and ev in ("UserPromptSubmit", "ManualNote"):
        ok, score = eval_rule(r["conception_candidate"], answers, variables)
        if ok and score >= 0.80:
            milestones.append({"type": "conception_candidate", "tier": 1, "score": round(score, 4)})
    if r.get("ai_origin_flag"):
        ok, score = eval_rule(r["ai_origin_flag"], answers, variables)
        if ok and score >= 0.50:
            milestones.append({"type": "ai_origin_element", "tier": 1, "score": round(score, 4)})
    if target and variables.get("maturity_delta", 0) >= 1 and rules.node_seq(target) < e["seq"]:
        rt = conn.execute("SELECT author FROM nodes WHERE node_id=?", (target,)).fetchone()
        if rt and rt["author"] == "human":
            p = answers["maturity"]["probabilities"].get(answers["maturity"]["choice"], 0.0)
            if p >= 0.80:
                milestones.append({"type": "maturity_jump", "tier": 1, "score": round(p, 4)})
    if "kills_approach" in answers and answers["kills_approach"]["p"] >= 0.80:
        milestones.append({"type": "discard_with_reason", "tier": 1, "score": round(answers["kills_approach"]["p"], 4)})
    stance = answers.get("stance") or {}
    if target and stance.get("choice") in STANCE_EDGE:
        p = min(stance["probabilities"].get(stance["choice"], 0.0),
                answers["target"]["probabilities"].get("n" + target, 0.0))
        if p >= 0.50:
            edges.append({"src": str(e["seq"]), "dst": target, "type": STANCE_EDGE[stance["choice"]], "p": round(p, 4)})
    for m in milestones:
        m["band"] = "review" if cfg["rules"]["review_band"][0] <= m["score"] < cfg["rules"]["review_band"][1] else "high"
    return {"answers": answers, "milestones": milestones, "edges": edges, "model_reported": reported,
            "candidates": candidates}


def run_tier1(store, entries, anns, provider=None, cfg=None):
    """Evaluate entries not yet annotated by Tier 1, and re-evaluate nodes whose
    status changed since their last Tier-1 annotation. Returns count written."""
    cfg = cfg or load_questions()
    conn = index.update(store)
    current = latest_by_target(anns, tier0=False)
    written = 0
    prev_ai = None
    by_seq = {e["seq"]: e for e in entries}
    for e in entries:
        ev = e.get("event")
        eligible = (e.get("actor") == "human" and ev in ("UserPromptSubmit", "ManualNote") and not e.get("consent")) \
            or ev in ("Stop", "SubagentStop", "PostToolUseFailure")
        if eligible:
            node = conn.execute("SELECT status FROM nodes WHERE node_id=?", (str(e["seq"]),)).fetchone()
            status = node["status"] if node else None
            prev = current.get(e["seq"])
            stale = prev is not None and (prev.get("context") or {}).get("status") != status
            if prev is None or stale:
                open_ideas = [r["label"] for r in conn.execute(
                    "SELECT n.label FROM nodes n JOIN entries x ON x.seq=n.seq WHERE n.author='human' AND n.status='open'"
                    " AND n.seq < ? AND (x.tags_json LIKE '%\"#idea\"%' OR x.tags_json LIKE '%\"#hypothesis\"%')"
                    " ORDER BY n.seq", (e["seq"],))]
                res = _tier1_one(store, conn, e, by_seq, cfg, provider, prev_ai, open_ideas)
                if res is not None:
                    write_annotation(store, {
                        "id": _new_id(), "ts": ledger.now_iso(), "target_seq": e["seq"], "target_hash": e["hash"],
                        "model": cfg.get("model"), "model_reported": res["model_reported"],
                        "questions_hash": cfg["_hash"], "answers": res["answers"], "milestones": res["milestones"],
                        "edges": res["edges"], "supersedes": prev["id"] if prev else None,
                        "context": {"status": status, "candidates": res["candidates"]}})
                    written += 1
        if ev in ("Stop", "SubagentStop"):
            prev_ai = rules.entry_text(e, store)
    conn.close()
    return written


# ---------------------------------------------------------------------------


def auto_enabled():
    import classifier

    return classifier.auto_enabled()


def run_once(store, provider=None, cfg=None, blob_cache=None, classifier=None):
    entries = [e for _, _, e in ledger.read_entries(store) if e]
    out = {"auto": 0, "auto_state": "off", "tier0": 0, "tier1": 0, "tier1_state": "off"}
    if classifier is not None or auto_enabled():
        import autoclass

        autoclass.run.last_backend = None
        try:
            out["auto"], out["auto_state"] = autoclass.run(store, entries, read_annotations(store), classifier)
        except Exception as exc:
            store.log_error("annotator.auto", exc)
            out["auto_state"] = "error"
            out["auto_error"] = str(exc)[:200]
        import classifier as clf

        out["classifier"] = clf.describe_backend(autoclass.run.last_backend)
    anns = read_annotations(store)
    out["tier0"] = run_tier0(store, entries, anns)
    if jev_enabled() or provider is not None:
        import jev_client

        if provider is None and not jev_client.has_key():
            out["tier1_state"] = "no-key"
        elif not find_consent(entries):
            out["tier1_state"] = "no-consent"
        else:
            out["tier1"] = run_tier1(store, entries, read_annotations(store), provider=provider, cfg=cfg)
            out["tier1_state"] = "on"
    conn = index.update(store)
    info = dict(out.get("classifier") or {}, state=out["auto_state"], error=out.get("auto_error"), checked=ledger.now_iso())
    index.store_status(conn, ledger.verify(store, blob_cache=blob_cache), len(index.review_queue(conn)), classifier=info)
    conn.close()
    return out


def daemon(store, interval=2.0, idle_exit_s=12 * 3600):
    pidfile = os.path.join(store.run, "annotator.pid")
    os.makedirs(store.run, exist_ok=True)
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    last_sig, idle_since, warned, blob_cache = None, time.time(), set(), {}
    try:
        while store.exists():
            try:
                st = os.stat(store.ledger)
                sig = (st.st_size, st.st_mtime)
            except OSError:
                sig = None
            if sig != last_sig:
                try:
                    res = run_once(store, blob_cache=blob_cache)
                    if res["tier1_state"] in ("no-key", "no-consent") and res["tier1_state"] not in warned:
                        store.log_error("annotator", RuntimeError(
                            "AUTHORSHIP_JEV=1 but Tier 1 is %s; run `annotator.py consent` in a terminal" % res["tier1_state"]))
                        warned.add(res["tier1_state"])
                except Exception as exc:
                    store.log_error("annotator", exc)
                last_sig, idle_since = sig, time.time()
            elif time.time() - idle_since > idle_exit_s:
                break
            time.sleep(interval)
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass


def consent(store, provider=None, stdin=None, stdout=None):
    """Human-only: show what Tier 1 sends where, and record a `yes` as a ManualNote."""
    import jev_client

    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    provider = provider or jev_client.default_provider()
    entries = [e for _, _, e in ledger.read_entries(store) if e]
    if find_consent(entries):
        stdout.write("Consent already recorded.\n")
        return 0
    text = (
        "Tier 1 annotation sends, for each human prompt and note, AI response and tool failure:\n"
        "  - the entry text (already redacted of secrets)\n"
        "  - the previous AI response and the list of open ideas\n"
        "  - up to 20 earlier elements as answer options\n"
        "to: %s (provider %s).\n"
        "This is text about your invention. Disclosure to a third party may matter for patentability;\n"
        "use a zero-data-retention setup, and keep AUTHORSHIP_JEV unset if in doubt.\n\n"
        "Type yes to allow it: " % (provider.endpoint, provider.name))
    stdout.write(text)
    stdout.flush()
    answer = stdin.readline().strip()
    if answer != "yes":
        stdout.write("Not recorded. Tier 1 stays off.\n")
        return 1
    e = ledger.write_note(store, "Consent: Tier 1 annotation may send ledger text to %s (%s)." % (
        provider.endpoint, provider.name), extra={"consent": {"scope": "jev", "provider": provider.name,
                                                             "endpoint": provider.endpoint}})
    stdout.write("Recorded as #%d.\n" % e["seq"])
    return 0


# ---------------------------------------------------------------------------
# Calibration


def calibration_report(labels, scores, thresholds=None):
    """labels: {seq: bool}; scores: {seq: float}. Precision and recall per threshold."""
    thresholds = thresholds or [0.5, 0.6, 0.7, 0.8, 0.9]
    rows = []
    for t in thresholds:
        tp = sum(1 for s, y in labels.items() if y and scores.get(s, 0) >= t)
        fp = sum(1 for s, y in labels.items() if not y and scores.get(s, 0) >= t)
        fn = sum(1 for s, y in labels.items() if y and scores.get(s, 0) < t)
        rows.append({"threshold": t, "precision": tp / (tp + fp) if tp + fp else None,
                     "recall": tp / (tp + fn) if tp + fn else None, "tp": tp, "fp": fp, "fn": fn})
    return rows


def conception_scores(anns, cfg):
    """Soft conception score per target: the best probability product the rule relies on."""
    out = {}
    for a in latest_by_target(anns, tier0=False).values():
        ans = a.get("answers") or {}
        ne = (ans.get("new_element") or {}).get("p", 0.0)
        st = ans.get("stance") or {}
        ps = (st.get("probabilities") or {}).get(st.get("choice"), 0.0) if st.get("choice") in ("originates", "modifies", "rejects") else 0.0
        out[a["target_seq"]] = min(ne, ps)
    return out


def calibrate(store, n=100, stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    cfg = load_questions()
    anns = read_annotations(store)
    scores = conception_scores(anns, cfg)
    entries = {e["seq"]: e for _, _, e in ledger.read_entries(store) if e}
    targets = [s for s in sorted(scores) if s in entries][:n]
    if not targets:
        stdout.write("No Tier-1 annotations to calibrate against. Enable Tier 1 first.\n")
        return 1
    labels = {}
    stdout.write("For each entry: is it a conception moment (the human introduces a new technical element)?\n"
                 "Answer y, n, or s to skip, q to stop.\n\n")
    for i, s in enumerate(targets, 1):
        stdout.write("[%d/%d] #%d  %s\n> " % (i, len(targets), s, rules.short(rules.entry_text(entries[s], store), 400)))
        stdout.flush()
        a = stdin.readline().strip().lower()
        if a == "q":
            break
        if a in ("y", "n"):
            labels[s] = a == "y"
    stdout.write("\nthreshold  precision  recall  (tp/fp/fn)\n")
    for r in calibration_report(labels, scores):
        fmt = lambda v: "   -  " if v is None else "%.2f" % v  # noqa: E731
        stdout.write("   %.2f      %s     %s   (%d/%d/%d)\n" % (r["threshold"], fmt(r["precision"]), fmt(r["recall"]),
                                                              r["tp"], r["fp"], r["fn"]))
    return 0


def main(argv):
    args = list(argv)
    project = ledger.project_dir()
    if "--project" in args:
        i = args.index("--project")
        project = os.path.abspath(args[i + 1])
        del args[i:i + 2]
    store = ledger.Store(project)
    cmd = args[0] if args else "run"
    if not store.exists():
        sys.stderr.write("no .authorship/ in %s\n" % project)
        return 1
    if cmd == "run":
        print(json.dumps(run_once(store)))
        return 0
    if cmd == "daemon":
        daemon(store)
        return 0
    if cmd == "consent":
        ledger.require_human("annotator consent")
        if not sys.stdin.isatty():
            sys.stderr.write("consent must be typed in a terminal\n")
            return 3
        return consent(store)
    if cmd == "calibrate":
        ledger.require_human("annotator calibrate")
        return calibrate(store)
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
