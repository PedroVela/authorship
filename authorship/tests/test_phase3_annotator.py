import io
import json
import os
import socket

import pytest

import annotator
import harness
import index
import jev_client
import ledger
import queries
from harness import entries


def tier0(store):
    return {a["target_seq"]: a for a in annotator.latest_by_target(annotator.read_annotations(store), tier0=True).values()}


def types(ann):
    return sorted(m["type"] for m in ann["milestones"])


def test_fixture_tier0_milestones(qr):
    annotator.run_once(qr)
    t = tier0(qr)
    assert types(t[2]) == ["problem_fixed", "stage_opened"]
    assert types(t[4]) == ["conception", "reduction_to_practice"]
    rtp = [m for m in t[4]["milestones"] if m["type"] == "reduction_to_practice"][0]
    assert rtp["evidence_seq"] == 8 and rtp["detail"] == ["src/recon.py"]
    assert types(t[10]) == ["discard_with_reason"]
    assert types(t[11]) == ["decision_with_reason", "stage_opened"]
    assert types(t[13]) == ["claim_candidate"]
    assert types(t[1]) == types(t[14]) == ["session_boundary"]
    assert set(t) == {1, 2, 4, 10, 11, 13, 14}
    for a in t.values():
        assert a["model"] == "tier0-rules-v1" and a["target_hash"] == ledger.find_entry(qr, a["target_seq"])["hash"]
    q = queries.Q(qr)
    rtp_rows = [m for m in q.milestones()["items"] if m["type"] == "reduction_to_practice"]
    assert rtp_rows == [dict(rtp_rows[0], seq=4, evidence_seq=8)]


def test_tier0_is_idempotent(qr):
    annotator.run_once(qr)
    before = open(qr.annotations).read()
    assert annotator.run_once(qr)["tier0"] == 0
    assert open(qr.annotations).read() == before


def test_retroactive_reduction_to_practice(project):
    snapshots = []

    def step(store):
        n = len(entries(store))
        if n in (7, 8):
            annotator.run_once(store)
            snapshots.append((n, types(tier0(store)[4])))

    store = harness.replay(project, on_step=step)
    assert snapshots[0] == (7, ["conception"])
    assert snapshots[-1] == (8, ["conception", "reduction_to_practice"])
    anns = [a for a in annotator.read_annotations(store) if a["target_seq"] == 4]
    assert len(anns) == 2 and anns[1]["supersedes"] == anns[0]["id"]
    # #4's own entry is untouched
    assert ledger.find_entry(store, 4)["hash"] == anns[0]["target_hash"] == anns[1]["target_hash"]


def test_ai_proposal_line_is_ai_origin_element(project):
    store = harness.init_store(project)
    t = os.path.join(project, "t.jsonl")
    with open(t, "w") as f:
        f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text":
                "Done.\n\nAI proposal: sign each statement page with an HMAC so tampering is detectable"}]}}) + "\n")
    harness.run_hook(project, {"hook_event_name": "Stop", "session_id": "s", "cwd": project, "transcript_path": t})
    annotator.run_once(store)
    (a,) = tier0(store).values()
    assert types(a) == ["ai_origin_element"] and "HMAC" in a["milestones"][0]["detail"]
    conn = index.update(store)
    assert conn.execute("SELECT author FROM nodes WHERE node_id='1.p1'").fetchone()[0] == "ai"


def test_ledger_bytes_unchanged_after_annotator(qr):
    before = open(qr.ledger, "rb").read()
    annotator.run_once(qr)
    annotator.run_once(qr, provider=jev_client.FakeProvider({}))  # no consent: Tier 1 must not run or write
    assert open(qr.ledger, "rb").read() == before


def test_jev_off_opens_zero_sockets(qr, monkeypatch):
    opened = []
    real = socket.socket

    class Spy(real):
        def __init__(self, *a, **k):
            opened.append(a)
            super().__init__(*a, **k)

    monkeypatch.delenv("AUTHORSHIP_JEV", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "would-be-used")
    monkeypatch.setattr(socket, "socket", Spy)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(OSError()))
    res = annotator.run_once(qr)
    assert res["tier1_state"] == "off"
    assert opened == []


# --- Tier 1 with the fake provider ------------------------------------------------


def with_consent(store):
    ledger.write_note(store, "Consent: test", extra={"consent": {"scope": "jev", "provider": "fake", "endpoint": "fake://jev"}})


def fake_for(entry4, other=None):
    """Answers keyed by entry text: entry #4 gets `entry4`, everything else `other`."""
    def answers(state, questions):
        base = entry4 if state["entry"].startswith("#idea instead of TTL") else (other or {})
        out = {}
        for name, q in questions.items():
            if name in base:
                out[name] = base[name]
            elif name == "target":
                out[name] = {"choice": "none", "probabilities": {"none": 1.0}}
            elif q["type"] == "noul":
                out[name] = {"noul": 0.1}
            elif q["type"] == "choice":
                first = list(q["criteria"])[-1]
                out[name] = {"choice": first, "probabilities": {first: 0.9}}
            elif q["type"] == "score":
                out[name] = {"score": 0.0, "probabilities": {"0": 1.0}}
        return out
    return answers


def target(node, p=0.9):
    return {"choice": "n" + node, "probabilities": {"n" + node: p}}


def run_fake(store, answers):
    with_consent(store)
    provider = jev_client.FakeProvider(answers)
    res = annotator.run_once(store, provider=provider)
    assert res["tier1_state"] == "on" and res["tier1"] > 0
    t1 = annotator.latest_by_target(annotator.read_annotations(store), tier0=False)
    return t1, provider


def test_tier1_high_band_conception_needs_confirmation(qr):
    t1, provider = run_fake(qr, fake_for({
        "new_element": {"noul": 0.93},
        "stance": {"choice": "modifies", "probabilities": {"originates": 0.08, "modifies": 0.81}},
        "maturity": {"score": 2.0, "probabilities": {"2": 0.9, "1": 0.1}},
        "target": target("3.3", 0.88),
    }))
    a = t1[4]
    assert a["model"] == "jev-1.13.0" and a["questions_hash"] == annotator.load_questions()["_hash"]
    assert a["answers"]["stance"]["probabilities"] == {"originates": 0.08, "modifies": 0.81}  # full vector kept
    assert [m["type"] for m in a["milestones"]] == ["conception_candidate"]
    assert a["milestones"][0]["score"] == 0.81 and a["milestones"][0]["band"] == "high"
    assert a["edges"] == [{"src": "4", "dst": "3.3", "type": "modifies", "p": 0.81}]
    assert "n3.3" in a["context"]["candidates"] or "3.3" in a["context"]["candidates"]
    conn = index.update(qr)
    row = conn.execute("SELECT confirmed FROM milestones WHERE target_seq=4 AND type='conception_candidate'").fetchone()
    assert row["confirmed"] == 0  # favors the human: always needs confirmation
    queue = index.review_queue(conn)
    labels = {(i["target_seq"], i["label"]) for i in queue}
    assert (4, "milestone:conception_candidate") in labels and (4, "edge:4:modifies:3.3") in labels
    # Choice option limit respected and candidates capped at 20
    for _, questions in provider.calls:
        if "target" in questions:
            assert len(questions["target"]["criteria"]) <= 21


def test_tier1_favorable_below_080_is_not_labeled(qr):
    t1, _ = run_fake(qr, fake_for({
        "new_element": {"noul": 0.75},
        "stance": {"choice": "modifies", "probabilities": {"modifies": 0.9}},
        "maturity": {"score": 2.0, "probabilities": {"2": 1.0}},
        "target": target("3.3"),
    }))
    assert t1[4]["milestones"] == []


def test_tier1_unfavorable_surfaces_at_050_in_review_band(qr):
    t1, _ = run_fake(qr, fake_for({
        "new_element": {"noul": 0.2},
        "stance": {"choice": "accepts", "probabilities": {"accepts": 0.55, "modifies": 0.3}},
        "maturity": {"score": 1.0, "probabilities": {"1": 1.0}},
        "target": target("3.3", 0.7),
    }))
    (m,) = t1[4]["milestones"]
    assert m["type"] == "ai_origin_element" and m["score"] == 0.55 and m["band"] == "review"
    assert t1[4]["edges"] == [{"src": "4", "dst": "3.3", "type": "derived_from", "p": 0.55}]
    conn = index.update(qr)
    assert any(i["label"] == "milestone:ai_origin_element" and i["target_seq"] == 4 for i in index.review_queue(conn))


def test_tier1_unfavorable_below_050_is_dropped(qr):
    t1, _ = run_fake(qr, fake_for({
        "stance": {"choice": "accepts", "probabilities": {"accepts": 0.45}},
        "target": target("3.3", 0.9),
    }))
    assert t1[4]["milestones"] == [] and t1[4]["edges"] == []


def test_tier1_response_act_and_kills_approach(qr):
    def answers(state, questions):
        out = {}
        if "response_act" in questions:
            out["response_act"] = {"choice": "unprompted_mechanism", "probabilities": {"unprompted_mechanism": 0.86}}
        if "kills_approach" in questions:
            out["kills_approach"] = {"noul": 0.91}
        return out

    t1, _ = run_fake(qr, answers)
    assert [m["type"] for m in t1[3]["milestones"]] == ["ai_origin_element"] and t1[3]["milestones"][0]["band"] == "high"
    assert [m["type"] for m in t1[6]["milestones"]] == ["discard_with_reason"]


def test_confirmation_resolves_review_item(qr):
    run_fake(qr, fake_for({
        "new_element": {"noul": 0.93},
        "stance": {"choice": "modifies", "probabilities": {"modifies": 0.81}},
        "maturity": {"score": 2.0, "probabilities": {"2": 1.0}},
        "target": target("3.3"),
    }))
    t1 = annotator.latest_by_target(annotator.read_annotations(qr), tier0=False)
    e4 = ledger.find_entry(qr, 4)
    ledger.write_confirm(qr, 4, e4["hash"], t1[4]["id"], "accept", "milestone:conception_candidate")
    ledger.write_confirm(qr, 4, e4["hash"], t1[4]["id"], "reject", "edge:4:modifies:3.3")
    conn = index.update(qr)
    assert conn.execute("SELECT confirmed FROM milestones WHERE target_seq=4 AND type='conception_candidate'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM edges WHERE src='4' AND dst='3.3'").fetchone()[0] == 0
    assert not [i for i in index.review_queue(conn) if i["target_seq"] == 4]


def test_tier1_reevaluates_when_status_changes(qr):
    ans = fake_for({"new_element": {"noul": 0.9}, "stance": {"choice": "originates", "probabilities": {"originates": 0.9}},
                    "maturity": {"score": 1.0, "probabilities": {"1": 1.0}}})
    run_fake(qr, ans)
    n_before = len(annotator.read_annotations(qr))
    # the human later confirms that #11 supersedes #4: #4 goes from adopted to superseded
    e11 = ledger.find_entry(qr, 11)
    ledger.write_confirm(qr, 11, e11["hash"], None, "accept", "edge:11:supersedes:4")
    annotator.run_once(qr, provider=jev_client.FakeProvider(ans))
    anns = annotator.read_annotations(qr)
    new = anns[n_before:]
    assert [a["target_seq"] for a in new if a["model"] != "tier0-rules-v1"] == [4]
    assert new[-1]["supersedes"] is not None


def test_no_key_no_consent_states(qr, monkeypatch):
    monkeypatch.setenv("AUTHORSHIP_JEV", "1")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert annotator.run_once(qr)["tier1_state"] == "no-key"
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    assert annotator.run_once(qr)["tier1_state"] == "no-consent"


def test_consent_flow_records_manual_note(qr):
    p = jev_client.FakeProvider({})
    out = io.StringIO()
    assert annotator.consent(qr, provider=p, stdin=io.StringIO("no\n"), stdout=out) == 1
    assert not annotator.find_consent(entries(qr))
    out = io.StringIO()
    assert annotator.consent(qr, provider=p, stdin=io.StringIO("yes\n"), stdout=out) == 0
    assert "fake://jev" in out.getvalue()
    note = annotator.find_consent(entries(qr))
    assert note["actor"] == "human" and note["event"] == "ManualNote" and note["consent"]["endpoint"] == "fake://jev"
    assert ledger.verify(qr)["ok"]


def test_consent_cli_is_human_only(qr):
    import subprocess
    import sys

    r = subprocess.run([sys.executable, os.path.join(harness.SCRIPTS, "annotator.py"), "consent", "--project", qr.project],
                       input="yes\n", capture_output=True, text=True, env=harness.hook_env(qr.project, CLAUDECODE="1"))
    assert r.returncode == 3 and not annotator.find_consent(entries(qr))


def test_rule_evaluator():
    ans = {"new_element": {"type": "noul", "p": 0.9},
           "stance": {"type": "choice", "choice": "modifies", "probabilities": {"modifies": 0.75}}}
    rule = "new_element >= 0.80 and stance in [originates, modifies, rejects] and p(stance) >= 0.70 and maturity_delta >= 1"
    assert annotator.eval_rule(rule, ans, {"maturity_delta": 1}) == (True, 0.75)
    assert annotator.eval_rule(rule, ans, {"maturity_delta": 0})[0] is False
    flag = "stance == accepts and p >= 0.50 or response_act == unprompted_mechanism and p >= 0.50"
    assert annotator.eval_rule(flag, ans)[0] is False
    ans["stance"] = {"type": "choice", "choice": "accepts", "probabilities": {"accepts": 0.6}}
    assert annotator.eval_rule(flag, ans) == (True, 0.6)


def test_tfidf_candidates_rank_related_nodes_first():
    docs = [("2", "reconciliation of QR payments is slow"), ("3.1", "batch per lot"), ("3.3", "TTL cache of the statement"),
            ("9", "unrelated words here")]
    top = annotator.tfidf_candidates("instead of TTL invalidate the cache by statement sequence", docs, k=2)
    assert top[0][0] == "3.3"


def test_calibration_report():
    rows = annotator.calibration_report({1: True, 2: True, 3: False, 4: False}, {1: 0.9, 2: 0.6, 3: 0.85, 4: 0.2},
                                        thresholds=[0.5, 0.8])
    assert rows[0] == {"threshold": 0.5, "precision": 2 / 3, "recall": 1.0, "tp": 2, "fp": 1, "fn": 0}
    assert rows[1]["precision"] == 0.5 and rows[1]["recall"] == 0.5


def test_calibrate_interactive(qr):
    run_fake(qr, fake_for({"new_element": {"noul": 0.93}, "stance": {"choice": "modifies", "probabilities": {"modifies": 0.9}},
                           "maturity": {"score": 2.0, "probabilities": {"2": 1.0}}}))
    out = io.StringIO()
    assert annotator.calibrate(qr, stdin=io.StringIO("y\n" + "n\n" * 30), stdout=out) == 0
    assert "precision" in out.getvalue()


@pytest.mark.parametrize("provider_cls,url_part", [(jev_client.TypesafeProvider, "systemone"),
                                                   (jev_client.VercelGatewayProvider, "evaluate")])
def test_real_providers_request_shape(monkeypatch, provider_cls, url_part):
    seen = {}

    def fake_post(url, key, body, timeout=30):
        seen.update(url=url, key=key, body=body)
        return {"model": "jev-1.13.0", "answers": {"q": {"type": "boolean", "probability": 0.7}}}

    monkeypatch.setattr(jev_client, "_post", fake_post)
    ans = jev_client.evaluate({"entry": "x"}, {"q": {"type": "noul", "instructions": "?"}}, provider=provider_cls(key="k"))
    assert url_part in seen["url"] and seen["key"] == "k" and ans["q"] == {"type": "noul", "p": 0.7}
    if provider_cls is jev_client.VercelGatewayProvider:
        assert seen["body"]["questions"]["q"]["type"] == "boolean"
        assert seen["body"]["providerOptions"]["gateway"]["zeroDataRetention"] is True
    else:
        assert seen["body"]["model"] == "jev-1.13.0"


def test_openrouter_provider_request_shape(monkeypatch):
    seen = {}

    def fake_post(url, key, body, timeout=30):
        seen.update(url=url, key=key, body=body)
        return {"model": "typesafe/jev-1.13-20260917", "provider": "TypeSafe",
                "answers": {"q": {"type": "noul", "noul": 0.8},
                            "c": {"type": "choice", "choice": "a", "confidence": 0.7, "probabilities": {"a": 0.7, "b": 0.3}}}}

    monkeypatch.setattr(jev_client, "_post", fake_post)
    ans = jev_client.evaluate({"entry": "x"}, {"q": {"type": "noul", "instructions": "?"},
                                               "c": {"type": "choice", "instructions": "?", "criteria": {"a": "A", "b": "B"}}},
                              provider=jev_client.OpenRouterProvider(key="k"), model="jev-1.13.0")
    assert seen["url"] == "https://openrouter.ai/api/alpha/decisions" and seen["key"] == "k"
    assert seen["body"]["model"] == "typesafe/jev-1.13"
    assert seen["body"]["questions"]["q"]["type"] == "noul"  # OpenRouter keeps TypeSafe's question types
    assert seen["body"]["provider"] == {"zdr": True, "data_collection": "deny", "allow_fallbacks": False}
    assert ans["q"] == {"type": "noul", "p": 0.8} and ans["c"]["probabilities"] == {"a": 0.7, "b": 0.3}
    assert ans["_model"] == "typesafe/jev-1.13-20260917"


def test_provider_choice_by_key(monkeypatch):
    for k in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "AI_GATEWAY_API_KEY", "AUTHORSHIP_JEV_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert jev_client.default_provider().name == "openrouter"
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")
    assert jev_client.default_provider().name == "openrouter"   # OpenRouter before Vercel
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    assert jev_client.default_provider().name == "typesafe"     # the maker first
    monkeypatch.setenv("AUTHORSHIP_JEV_PROVIDER", "openrouter")
    assert jev_client.default_provider().name == "openrouter"
    assert jev_client.openrouter_model("jev-1.13.0") == "typesafe/jev-1.13"
