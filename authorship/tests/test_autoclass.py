"""Automatic classification: the untagged QR session must come out like the tagged one."""
import json
import os
import stat
import subprocess
import sys

import pytest

import annotator
import autoclass
import classifier as clf
import drafts
import harness
import index
import ledger
import queries
import validate_citations
from harness import entries


def item(seq, kind, kc=0.92, **kw):
    base = {"seq": seq, "kind": kind, "kind_confidence": kc, "new_element": 0.0, "stance": "unrelated",
            "stance_confidence": 0.0, "target": "none", "target_confidence": 0.0, "builds_on": [],
            "maturity": "goal", "maturity_confidence": 0.0, "ai_act": "explains", "ai_act_confidence": 0.0,
            "new_stage": "", "summary": "s"}
    base.update(kw)
    return base


def qr_answers(overrides=None):
    """A fake classifier that reads the QR session the way a good model would."""
    overrides = overrides or {}

    def answer(payload):
        out = []
        for e in payload["entries"]:
            t = e["text"]
            if "takes 40 s" in t:
                a = item(e["seq"], "problem", 0.95, new_stage="Exploration")
            elif t.startswith("instead of TTL"):
                a = item(e["seq"], "idea", 0.93, new_element=0.9, stance="modifies", stance_confidence=0.86,
                         target="3.3", target_confidence=0.9, builds_on=[{"id": "2", "confidence": 0.8}],
                         maturity="mechanism", maturity_confidence=0.8)
            elif t.startswith("Bloom filter"):
                a = item(e["seq"], "discard", 0.9)
            elif t.startswith("long-poll"):
                a = item(e["seq"], "decision", 0.91, new_stage="Prototype", stance="rejects", stance_confidence=0.88,
                         target="3.2", target_confidence=0.9, builds_on=[{"id": "4", "confidence": 0.85}])
            elif t.startswith("method that detects"):
                a = item(e["seq"], "claim", 0.9, builds_on=[{"id": "4", "confidence": 0.9}, {"id": "11", "confidence": 0.85}])
            elif e["who"] == "AI" and "Three ways" in t:
                a = item(e["seq"], "ai", 0.9, ai_act="offers_alternatives", ai_act_confidence=0.95)
            else:
                a = item(e["seq"], "ai" if e["who"] == "AI" else "instruction", 0.9, ai_act="implements_instruction",
                         ai_act_confidence=0.9)
            for key, fn in overrides.items():
                if key in t:
                    a = fn(a)
            out.append(a)
        return out

    return answer


@pytest.fixture
def untagged(project):
    return harness.replay(project, "qr_untagged")


def run_auto(store, answers=None):
    fake = clf.FakeClassifier(answers or qr_answers())
    res = annotator.run_once(store, classifier=fake)
    return res, fake


def test_untagged_session_is_classified_without_any_tag(untagged):
    assert all(not e.get("tags") for e in entries(untagged))
    res, fake = run_auto(untagged)
    assert res["auto_state"] == "on" and res["auto"] == 7  # 4 prompts, 1 note, 2 AI replies
    conn = index.update(untagged)
    auto = {r["seq"]: json.loads(r["auto_tags_json"]) for r in conn.execute("SELECT seq, auto_tags_json FROM entries")}
    assert auto[2] == ["#problem"] and auto[4] == ["#idea"] and auto[10] == ["#discard"]
    assert auto[11] == ["#decision"] and auto[13] == ["#claim"]
    stages = [tuple(r) for r in conn.execute(
        "SELECT stage, MIN(seq), MAX(seq) FROM entries WHERE stage IS NOT NULL GROUP BY stage ORDER BY 2")]
    assert stages == [("Exploration", 2, 10), ("Prototype", 11, 13)]
    ibis = {r["node_id"]: r["ibis_type"] for r in conn.execute("SELECT node_id, ibis_type FROM nodes")}
    assert (ibis["2"], ibis["4"], ibis["11"], ibis["13"]) == ("issue", "position", "decision", "claim")
    status = {r["node_id"]: r["status"] for r in conn.execute("SELECT node_id, status FROM nodes")}
    assert status["10"] == "discarded" and status["3.2"] == "rejected" and status["3.3"] == "modified"


def test_milestones_count_automatically_and_nothing_waits_for_review(untagged):
    run_auto(untagged)
    q = queries.Q(untagged)
    ms = {(m["seq"], m["type"]): m["confirmation"] for m in q.milestones()["items"] if m["type"] != "session_boundary"}
    assert ms[(2, "problem_fixed")] == "automatic"
    assert ms[(4, "conception_candidate")] == "automatic"
    assert ms[(10, "discard_with_reason")] == "automatic"
    assert ms[(11, "decision_with_reason")] == "automatic"
    assert ms[(13, "claim_candidate")] == "automatic"
    assert (4, "reduction_to_practice") in ms  # the green-after-red rule finds the untagged idea
    assert index.review_queue(q.conn) == []


def test_curated_lineage_matches_the_tagged_and_confirmed_one(untagged):
    run_auto(untagged)
    q = queries.Q(untagged)
    assert set(index.lineage(q.conn, "13", curated=True)) == {"2", "3.3", "4", "11", "13"}
    s = q.lineage("13", curated=True)["summary"]
    assert [(a["node"], a["modified_by_human_at"]) for a in s["ai_elements"]] == [("3.3", ["4"])]


def test_disclosure_from_untagged_session_passes_citations(untagged):
    run_auto(untagged)
    path = drafts.disclosure(untagged)
    n, errors = validate_citations.validate(path, untagged.project)
    assert errors == [] and n > 10
    text = open(path).read()
    assert "automatically classified edges" in text
    assert "> method that detects the absence of new transactions" in text
    assert "| TTL cache: cache the bank's statement for 30 s and reconcile against the cache. | AI |" in text
    assert "(automatic)" in text


def test_asymmetric_thresholds(untagged):
    weak_idea = {"instead of TTL": lambda a: dict(a, kind_confidence=0.65, stance="accepts", stance_confidence=0.55,
                                                  target_confidence=0.9)}
    run_auto(untagged, qr_answers(weak_idea))
    q = queries.Q(untagged)
    ms = {(m["seq"], m["type"]): m["confirmation"] for m in q.milestones()["items"]}
    assert ms[(4, "conception_candidate")] == "pending"   # favors the human, below 0.80: waits for review
    assert ms[(4, "ai_origin_element")] == "automatic"    # against the human, from 0.50: counts
    queue = index.review_queue(q.conn)
    assert [(i["target_seq"], i["label"]) for i in queue] == [(4, "milestone:conception_candidate")]
    # below 0.70 the classifier does not tag
    assert json.loads(q.conn.execute("SELECT auto_tags_json FROM entries WHERE seq=4").fetchone()[0]) == []


def test_human_tags_win(qr):
    """On the tagged session, the classifier never overrides what the human typed."""
    contrary = {"instead of TTL": lambda a: dict(a, kind="instruction")}
    run_auto(qr, qr_answers(contrary))
    conn = index.update(qr)
    assert conn.execute("SELECT auto_tags_json FROM entries WHERE seq=4").fetchone()[0] == "[]"
    assert conn.execute("SELECT ibis_type FROM nodes WHERE node_id='4'").fetchone()[0] == "position"
    stages = [tuple(r) for r in conn.execute(
        "SELECT stage, MIN(seq), MAX(seq) FROM entries WHERE stage IS NOT NULL GROUP BY stage ORDER BY 2")]
    assert stages == [("Exploration", 2, 10), ("Prototype", 11, 13)]


def test_runs_once_per_entry_and_never_touches_the_ledger(untagged):
    before = open(untagged.ledger, "rb").read()
    _, fake = run_auto(untagged)
    calls = len(fake.calls)
    assert calls == 2  # 7 entries in batches of 6
    res, fake2 = run_auto(untagged)
    assert res["auto"] == 0 and fake2.calls == []
    assert open(untagged.ledger, "rb").read() == before
    a = [x for x in annotator.read_annotations(untagged) if x.get("method") == "auto"]
    assert all(x["model"] == "fake-classifier" and x["questions_hash"] == clf.PROMPT_HASH for x in a)
    assert all("kind_confidence" in x["answers"] for x in a)


def test_classifier_failure_is_logged_and_retried(untagged):
    def boom(payload):
        raise clf.ClassifierError("rate limited")

    res = annotator.run_once(untagged, classifier=clf.FakeClassifier(boom))
    assert res["auto_state"] == "error" and res["tier0"] > 0
    assert "rate limited" in open(untagged.errors).read()
    res, _ = run_auto(untagged)
    assert res["auto"] == 7


def test_rebuild_matches_incremental_with_auto(untagged):
    run_auto(untagged)
    conn = index.update(untagged)
    dump = lambda c: {t: sorted(tuple(r) for r in c.execute("SELECT * FROM %s" % t))  # noqa: E731
                      for t in ("entries", "nodes", "edges", "milestones")}
    a = dump(conn)
    assert a == dump(index.update(untagged, rebuild=True))


@pytest.mark.skipif(os.name == "nt", reason="the fake `claude` is a shebang script")
def test_claude_cli_backend_is_sandboxed(untagged, tmp_path, monkeypatch):
    """The real backend runs `claude -p` with no tools, hooks or MCP, outside the project."""
    log = tmp_path / "argv.json"
    fake = tmp_path / "claude"
    fake.write_text("#!%s\nimport json, os, sys\n"
                    "json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env': dict(os.environ)}, open(%r, 'w'))\n"
                    "payload = json.loads(sys.argv[-1])\n"
                    "out = [{'seq': e['seq'], 'kind': 'idea' if e['who'] == 'human' else 'ai', 'kind_confidence': 0.9,"
                    " 'new_element': 0.9, 'stance': 'originates', 'stance_confidence': 0.9, 'target': 'none',"
                    " 'target_confidence': 0, 'builds_on': [], 'maturity': 'approach', 'maturity_confidence': 0.8,"
                    " 'ai_act': 'explains', 'ai_act_confidence': 0.9, 'new_stage': '', 'summary': 'x'} for e in payload['entries']]\n"
                    "print(json.dumps({'is_error': False, 'structured_output': {'entries': out},"
                    " 'modelUsage': {'claude-haiku-4-5-20251001': {}}, 'total_cost_usd': 0.001}))\n"
                    % (sys.executable, str(log)))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("AUTHORSHIP_CLAUDE_BIN", str(fake))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("AUTHORSHIP_AUTO", "1")
    res = annotator.run_once(untagged)
    assert res["auto_state"] == "on" and res["auto"] == 7
    seen = json.load(open(str(log)))
    argv = seen["argv"]
    assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--settings") + 1]) == {"disableAllHooks": True}
    assert argv[argv.index("--model") + 1] == clf.DEFAULT_MODEL and "--no-session-persistence" in argv
    assert argv[argv.index("--setting-sources") + 1] == "" and seen["env"]["MAX_THINKING_TOKENS"] == "0"
    assert not seen["cwd"].startswith(untagged.project) and "CLAUDECODE" not in seen["env"]
    assert "AUTHORSHIP_PROJECT_DIR" not in seen["env"] and seen["env"]["AUTHORSHIP_HINT"] == "0"
    a = [x for x in annotator.read_annotations(untagged) if x.get("method") == "auto"]
    assert a[0]["model"] == "claude-haiku-4-5-20251001"


def test_off_switch_and_missing_cli(untagged, monkeypatch):
    monkeypatch.setenv("AUTHORSHIP_AUTO", "0")
    assert annotator.run_once(untagged)["auto_state"] == "off"
    monkeypatch.setenv("AUTHORSHIP_AUTO", "1")
    monkeypatch.setenv("AUTHORSHIP_CLAUDE_BIN", "/nonexistent")
    monkeypatch.delenv("CLAUDE_CODE_EXECPATH", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    for k in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "AI_GATEWAY_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert annotator.run_once(untagged)["auto_state"] == "no-backend"


@pytest.mark.skipif(os.environ.get("AUTHORSHIP_TEST_LIVE") != "1", reason="set AUTHORSHIP_TEST_LIVE=1 to call the real model")
def test_live_classifier_on_untagged_session(untagged, monkeypatch):
    monkeypatch.setenv("AUTHORSHIP_AUTO", "1")
    res = annotator.run_once(untagged)
    assert res["auto_state"] == "on" and res["auto"] == 7
    conn = index.update(untagged)
    auto = {r["seq"]: json.loads(r["auto_tags_json"]) for r in conn.execute("SELECT seq, auto_tags_json FROM entries")}
    print(auto)
    assert auto[2] == ["#problem"] and auto[4] in (["#idea"], ["#decision"]) and auto[13] == ["#claim"]
    ms = {(m["seq"], m["type"]) for m in queries.Q(untagged).milestones()["items"]}
    assert (4, "conception_candidate") in ms and (4, "reduction_to_practice") in ms
    assert set(index.lineage(conn, "13", curated=True)) >= {"3.3", "4", "11", "13"}


def test_conception_when_the_model_calls_the_idea_a_decision(untagged):
    as_decision = {"instead of TTL": lambda a: dict(a, kind="decision", kind_confidence=0.9)}
    run_auto(untagged, qr_answers(as_decision))
    ms = {(m["seq"], m["type"]): m["confirmation"] for m in queries.Q(untagged).milestones()["items"]}
    assert ms[(4, "conception_candidate")] == "automatic" and ms[(4, "decision_with_reason")] == "automatic"
    assert (4, "reduction_to_practice") in ms


def jev_fake(raw):
    """A fake Jev endpoint: raw(state, questions) -> Jev wire answers."""
    import jev_client
    return jev_client.FakeProvider(raw)


def jev_answers(state, questions):
    t = state["entry"]["text"]
    out = {}
    def choice(name, pick, p=0.9):
        out[name] = {"choice": pick, "probabilities": {pick: p, "other": round(1 - p, 4)}}
    if "kind" in questions:
        kind = ("problem" if "takes 40 s" in t else "idea" if t.startswith("instead of TTL") else
                "discard" if t.startswith("Bloom") else "decision" if t.startswith("long-poll") else
                "claim" if t.startswith("method that") else "instruction")
        choice("kind", kind)
        out["new_element"] = {"noul": 0.9 if kind in ("idea", "claim", "decision") else 0.2}
        choice("stance", "modifies" if kind == "idea" else "originates", 0.85)
        if "target" in questions:
            tgt = "n3.3" if kind == "idea" else "n4" if kind in ("claim", "decision") else "none"
            out["target"] = {"choice": tgt, "probabilities": {tgt: 0.6, "n11": 0.35} if kind == "claim" else {tgt: 0.9}}
        out["maturity"] = {"score": 2.0, "probabilities": {"2": 0.9, "1": 0.1}}
        stage = "Exploration" if kind == "problem" else "Prototype" if kind == "decision" else "same"
        choice("stage", stage, 0.8)
    if "ai_act" in questions:
        choice("ai_act", "offers_alternatives" if "Three ways" in t else "implements_instruction")
    return out


def test_jev_backend_drives_the_same_automatic_pipeline(untagged, monkeypatch):
    fake = jev_fake(jev_answers)
    backend = clf.JevBackend(provider=fake)
    res = annotator.run_once(untagged, classifier=backend)
    assert res["auto"] == 7 and len(fake.calls) == 7  # one Jev call per entry
    conn = index.update(untagged)
    auto = {r[0]: json.loads(r[1]) for r in conn.execute("SELECT seq, auto_tags_json FROM entries WHERE auto_tags_json != '[]'")}
    assert auto == {2: ["#problem"], 4: ["#idea"], 10: ["#discard"], 11: ["#decision"], 13: ["#claim"]}
    stages = [tuple(r) for r in conn.execute(
        "SELECT stage, MIN(seq), MAX(seq) FROM entries WHERE stage IS NOT NULL GROUP BY stage ORDER BY 2")]
    assert stages == [("Exploration", 2, 10), ("Prototype", 11, 13)]
    a = [x for x in annotator.read_annotations(untagged) if x.get("method") == "auto"]
    assert all(x["backend"] == "jev" and x["model"] == "jev-1.13.0" for x in a)
    a13 = [x for x in a if x["target_seq"] == 13][0]
    assert a13["answers"]["probabilities"]["kind"] == {"claim": 0.9, "other": 0.1}  # full vectors kept
    assert {b["id"] for b in a13["answers"]["builds_on"]} == {"4", "11"}
    assert {"11", "13", "4", "3.3"} <= set(index.lineage(conn, "13", curated=True))


def test_backend_selection(monkeypatch):
    for k in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "AI_GATEWAY_API_KEY", "AUTHORSHIP_AUTO_BACKEND", "AUTHORSHIP_JEV_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AUTHORSHIP_CLAUDE_BIN", sys.executable)  # any executable stands in for claude
    assert isinstance(clf.default_backend(), clf.ClaudeCLI)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")
    b = clf.default_backend()
    assert isinstance(b, clf.JevBackend) and b.provider.name == "vercel_gateway"
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    b = clf.default_backend()
    assert b.provider.name == "openrouter" and "OpenRouter" in clf.describe_backend(b)["sends_to"]
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    assert clf.default_backend().provider.name == "typesafe"
    monkeypatch.setenv("AUTHORSHIP_AUTO_BACKEND", "claude")
    assert isinstance(clf.default_backend(), clf.ClaudeCLI)


def test_log_shows_automatic_tags(untagged, capsys, monkeypatch):
    import cli
    run_auto(untagged)
    monkeypatch.chdir(untagged.project)
    cli.main(["log"])
    out = capsys.readouterr().out
    assert "#idea~  instead of TTL" in out and "#claim~  method that detects" in out and "set automatically" in out


def test_review_all_lets_the_human_correct_automatic_labels(untagged):
    import io
    import cli
    run_auto(untagged)
    out = io.StringIO()
    cli.cmd_review(untagged, ["--all", "--list"], stdout=out)
    listed = out.getvalue()
    assert "#13 is" in listed and "(automatic)" in listed and "#4 modifies #3.3" in listed
    conn = index.update(untagged)
    queue = index.review_queue(conn, include_auto=True)
    first = queue[0]
    answers = "".join("r\n" if i == 0 else "s\n" for i in range(len(queue)))
    cli.cmd_review(untagged, ["--all"], stdin=io.StringIO(answers), stdout=io.StringIO())
    e = entries(untagged)[-1]
    assert e["event"] == "Confirm" and e["decision"] == "reject" and e["label"] == first["label"]
    left = {(i["target_seq"], i["label"]) for i in index.review_queue(index.update(untagged), include_auto=True)}
    assert (first["target_seq"], first["label"]) not in left


def test_status_explains_the_classifier(untagged, capsys, monkeypatch):
    import cli
    monkeypatch.setenv("AUTHORSHIP_AUTO", "0")
    monkeypatch.chdir(untagged.project)
    cli.main(["status"])
    assert "classifier: off (AUTHORSHIP_AUTO=0)" in capsys.readouterr().out


def test_saved_choice_selects_backend_and_model(monkeypatch):
    for k in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "AI_GATEWAY_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AUTHORSHIP_CLAUDE_BIN", sys.executable)
    clf.save_choice({"backend": "claude", "claude_model": "haiku"})
    b = clf.default_backend()
    assert isinstance(b, clf.ClaudeCLI) and b.model == "haiku"
    monkeypatch.setenv("AUTHORSHIP_AUTO_MODEL", "opus")  # the environment wins
    assert clf.default_backend().model == "opus"
    monkeypatch.delenv("AUTHORSHIP_AUTO_MODEL")
    # a Jev key does not override an explicit choice of Claude
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    assert isinstance(clf.default_backend(), clf.ClaudeCLI)
    # OpenRouter as a plain LLM provider, any model
    clf.save_choice({"backend": "openrouter", "openrouter_model": "google/gemini-3.8-flash"})
    with pytest.raises(clf.ClassifierError):
        clf.default_backend()  # no OPENROUTER_API_KEY yet
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    b = clf.default_backend()
    d = clf.describe_backend(b)
    assert isinstance(b, clf.OpenRouterLLM) and b.model == "google/gemini-3.8-flash"
    assert d["third_party"] and "gemini" in d["sends_to"]
    # Jev: provider and model
    clf.save_choice({"backend": "jev", "jev_provider": "openrouter", "jev_model": "typesafe/jev-latest"})
    b = clf.default_backend()
    assert isinstance(b, clf.JevBackend) and b.provider.name == "openrouter" and b.model == "typesafe/jev-latest"
    monkeypatch.setenv("AUTHORSHIP_JEV_MODEL", "jev-1.14.0")
    assert clf.default_backend().model == "jev-1.14.0"
    # off
    monkeypatch.delenv("AUTHORSHIP_AUTO")
    clf.save_choice({"backend": "off"})
    assert not clf.auto_enabled() and not annotator.auto_enabled()
    monkeypatch.setenv("AUTHORSHIP_AUTO", "1")
    assert clf.auto_enabled()
    clf.save_choice({})
    assert clf.choice() == {}


def test_openrouter_llm_sends_strict_schema_and_parses(untagged, monkeypatch):
    import jev_client
    sent = {}

    def fake_post(url, key, body, timeout=30):
        sent.update(url=url, key=key, body=body)
        payload = json.loads(body["messages"][1]["content"])
        out = [{"seq": e["seq"], "kind": "idea" if e["who"] == "human" else "ai", "kind_confidence": 0.9,
                "new_element": 0.9, "stance": "originates", "stance_confidence": 0.9, "target": "none",
                "target_confidence": 0, "builds_on": [], "maturity": "approach", "maturity_confidence": 0.8,
                "ai_act": "explains", "ai_act_confidence": 0.9, "new_stage": "", "summary": "x"} for e in payload["entries"]]
        return {"model": "google/gemini-3.8-flash", "usage": {"cost": 0.0001},
                "choices": [{"message": {"content": json.dumps({"entries": out})}}]}

    monkeypatch.setattr(jev_client, "_post", fake_post)
    b = clf.OpenRouterLLM(model="google/gemini-3.8-flash", key="k")
    res = annotator.run_once(untagged, classifier=b)
    assert res["auto_state"] == "on" and res["auto"] == 7
    body = sent["body"]
    assert sent["url"].endswith("/chat/completions") and body["model"] == "google/gemini-3.8-flash"
    assert body["provider"] == {"zdr": True, "data_collection": "deny", "require_parameters": True}
    schema = body["response_format"]["json_schema"]["schema"]
    assert body["response_format"]["json_schema"]["strict"] and schema["additionalProperties"] is False
    assert schema["properties"]["entries"]["items"]["additionalProperties"] is False
    assert body["messages"][0]["content"] == clf.SYSTEM_PROMPT


def test_openrouter_model_list_keeps_structured_output_models():
    data = {"data": [
        {"id": "anthropic/claude-sonnet-5", "supported_parameters": ["structured_outputs"], "pricing": {"prompt": "0.000002"}},
        {"id": "anthropic/claude-sonnet-5:batch", "supported_parameters": ["structured_outputs"], "pricing": {"prompt": "0.000001"}},
        {"id": "some/model-without-json", "supported_parameters": ["tools"], "pricing": {"prompt": "0"}},
        {"id": "typesafe/jev-router", "supported_parameters": ["structured_outputs"], "pricing": {"prompt": "-1"}}]}
    assert clf.openrouter_models(fetch=lambda: data) == [("anthropic/claude-sonnet-5", 2.0), ("typesafe/jev-router", None)]
    assert clf.openrouter_models("claude", fetch=lambda: data) == [("anthropic/claude-sonnet-5", 2.0)]
