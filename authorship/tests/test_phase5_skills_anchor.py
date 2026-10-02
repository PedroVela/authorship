import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import anchor
import annotator
import drafts
import harness
import ledger
import validate_citations
from harness import PLUGIN, SCRIPTS, TESTS, entries

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not installed")

TSA_CNF = """
[ req ]
distinguished_name = dn
prompt = no
[ dn ]
CN = test
[ v3_ca ]
basicConstraints = critical,CA:true
keyUsage = critical,keyCertSign,cRLSign
[ v3_tsa ]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature,nonRepudiation
extendedKeyUsage = critical,timeStamping
[ tsa_config ]
dir = .
serial = ./serial
crypto_device = builtin
signer_cert = ./tsa.crt
signer_key = ./tsa.key
default_policy = 1.2.3.4.1
digests = sha256
accuracy = secs:1
ordering = yes
tsa_name = no
ess_cert_id_chain = no
"""


# --- disclosure and contribution --------------------------------------------


def test_disclosure_on_fixture_passes_validate_citations(qr_confirmed):
    annotator.run_once(qr_confirmed)
    path = drafts.disclosure(qr_confirmed)
    assert path.startswith(os.path.join(qr_confirmed.project, "authorship-exports"))
    assert path.endswith("-disclosure.md")
    r = subprocess.run([sys.executable, os.path.join(TESTS, "validate_citations.py"), path, "--project", qr_confirmed.project],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    text = open(path).read()
    for n, title in enumerate(["Problem", "Alternatives considered", "The invention", "Elements",
                               "Reduction-to-practice evidence", "Discarded approaches", "Timeline",
                               "Chain and anchor status"], 1):
        assert "## %d. %s" % (n, title) in text
    # human text quoted verbatim
    assert "> #idea instead of TTL, invalidate by the statement sequence number" in text
    assert "| TTL cache: cache the bank's statement for 30 s and reconcile against the cache. | AI |" in text
    assert re.search(r"\| #idea instead of TTL.* \| mixed \| #4 \([0-9a-f]{12}\), #3\.3 \(", text)
    assert "#8 (" in text  # reduction-to-practice evidence cited


def test_validate_citations_catches_bad_citations(qr):
    path = os.path.join(qr.project, "bad.md")
    good = ledger.find_entry(qr, 4)["hash"][:12]
    open(path, "w").write("# x\n\nDraft for attorney review. Not legal advice.\n\n#4 (%s) ok, #4 (000000000000) wrong, "
                          "#99 (%s) missing, #3.9 (%s) no option, and a bare #4.\n" % (good, good, ledger.find_entry(qr, 3)["hash"][:12]))
    n, errors = validate_citations.validate(path, qr.project)
    assert n == 4 and len(errors) == 4
    open(path, "w").write("#4 (%s)\n" % good)
    assert any("header" in e for e in validate_citations.validate(path, qr.project)[1])


def test_contribution_lists_ai_origin_facts_before_human_ones(qr_confirmed):
    path = drafts.contribution(qr_confirmed, "13")
    text = open(path).read()
    n, errors = validate_citations.validate(path, qr_confirmed.project, contribution=True)
    assert errors == [] and n > 0
    ai = text.index("AI-originated element #3.3")
    assert ai < text.index("Human-originated elements") < text.index("> #idea instead of TTL")
    assert "The human modified it at #4" in text
    # the subagent is instructed in the same order
    agent = open(os.path.join(PLUGIN, "agents", "inventorship-reviewer.md")).read()
    assert agent.index("Unfavorable facts") < agent.index("Human-originated elements") < agent.index("Gaps where")


def test_skills_and_agent_frontmatter():
    for name in ("init", "disclosure", "review", "seal"):
        text = open(os.path.join(PLUGIN, "skills", name, "SKILL.md")).read()
        fm = text.split("---")[1]
        assert re.search(r"^name: %s$" % name, fm, re.M) and re.search(r"^description: .{40,}", fm, re.M)
        assert "${CLAUDE_PLUGIN_ROOT}/scripts/" in text
    seal = open(os.path.join(PLUGIN, "skills", "seal", "SKILL.md")).read()
    assert "anchor.py seal" in seal
    agent = open(os.path.join(PLUGIN, "agents", "inventorship-reviewer.md")).read().split("---")[1]
    assert "tools: mcp__plugin_authorship_authorship__*, Read, Write" in agent


# --- anchoring with a fake TSA ---------------------------------------------------


@pytest.fixture(scope="module")
def fake_tsa(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("tsa"))
    open(os.path.join(d, "tsa.cnf"), "w").write(TSA_CNF)
    open(os.path.join(d, "serial"), "w").write("01\n")

    def ossl(*args):
        subprocess.run(["openssl"] + list(args), cwd=d, check=True, capture_output=True)

    ossl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.crt", "-days", "30",
         "-subj", "/CN=Test CA", "-config", "tsa.cnf", "-extensions", "v3_ca")
    ossl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "tsa.key", "-out", "tsa.csr", "-subj", "/CN=Test TSA",
         "-config", "tsa.cnf")
    ossl("x509", "-req", "-in", "tsa.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-out", "tsa.crt",
         "-days", "30", "-extfile", "tsa.cnf", "-extensions", "v3_tsa")
    hits = []
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            with lock:
                hits.append(self.headers.get("Content-Type"))
                q = os.path.join(d, "q.tsq")
                open(q, "wb").write(body)
                ossl("ts", "-reply", "-queryfile", "q.tsq", "-config", "tsa.cnf", "-section", "tsa_config", "-out", "r.tsr")
                data = open(os.path.join(d, "r.tsr"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/timestamp-reply")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {"AUTHORSHIP_TSA": "http://127.0.0.1:%d/tsr" % srv.server_address[1], "AUTHORSHIP_TSA_CAFILE": os.path.join(d, "ca.crt"),
           "AUTHORSHIP_TSA_CERT": os.path.join(d, "tsa.crt"), "AUTHORSHIP_OTS": "0"}
    yield {"env": env, "hits": hits}
    srv.shutdown()


@pytest.fixture
def tsa_env(fake_tsa, monkeypatch):
    for k, v in fake_tsa["env"].items():
        monkeypatch.setenv(k, v)
    return fake_tsa


def anchors_of(store):
    return [e for e in entries(store) if e["event"] == "Anchor"]


def test_anchor_with_fake_tsa_pending_then_complete(qr, tsa_env):
    head_seq, head_hash = ledger.read_head(qr)
    res = anchor.run(qr)
    assert res["errors"] == {} and res["completed"] == ["rfc3161"]
    base = os.path.join(qr.anchors, "%d-%s" % (head_seq, head_hash[:16]))
    for ext in (".txt", ".tsq", ".tsr"):
        assert os.path.getsize(base + ext) > 0
    assert "application/timestamp-query" in tsa_env["hits"]
    pending, complete = anchors_of(qr)
    assert pending["status"] == "pending" and pending["anchored_seq"] == head_seq and pending["anchored_hash"] == head_hash
    assert complete["status"] == "complete" and complete["completed_methods"] == ["rfc3161"]
    assert complete["pending_seq"] == pending["seq"]
    v = ledger.verify(qr)
    assert v["ok"] and v["sealed_upto"] == head_seq and v["unsealed"] == 2
    a = anchor.verify_anchors(qr)
    assert a["ok"] and a["details"][0]["rfc3161"] == "verified"


def test_verify_anchors_fails_when_whole_chain_is_rewritten(qr, tsa_env):
    anchor.run(qr)
    # rewrite every entry from #4 on, recomputing all hashes: the chain alone still verifies
    lines = [json.loads(l) for l in open(qr.ledger)]
    prev = None
    out = []
    for e in lines:
        if e["seq"] == 4:
            e["text"] = "#idea something else entirely"
            e["sha256"] = ledger.sha256_text(e["text"])
        if prev is not None:
            e["prev"] = prev
        e["hash"] = ledger.entry_hash(e)
        prev = e["hash"]
        out.append(ledger.canonical_json(e))
    open(qr.ledger, "w").write("\n".join(out) + "\n")
    assert ledger.verify(qr)["ok"]
    res = anchor.verify_anchors(qr)
    assert not res["ok"] and "does not match the recomputed ledger" in res["reason"]
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "ledger.py"), "verify", "--anchors", "--project", qr.project],
                       capture_output=True, text=True, env=harness.hook_env(qr.project, **tsa_env["env"]))
    assert r.returncode == 1 and "BROKEN" in r.stdout


def test_tampered_tsr_fails_verification(qr, tsa_env):
    anchor.run(qr)
    tsr = [os.path.join(qr.anchors, n) for n in os.listdir(qr.anchors) if n.endswith(".tsr")][0]
    data = bytearray(open(tsr, "rb").read())
    data[-40] ^= 0xFF
    open(tsr, "wb").write(bytes(data))
    assert not anchor.verify_anchors(qr)["ok"]


def test_seal_runs_detached_and_reports(qr, tsa_env):
    env = harness.hook_env(qr.project, **tsa_env["env"])
    t = time.perf_counter()
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "anchor.py"), "seal", "--project", qr.project],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and time.perf_counter() - t < 2.0
    assert "Sealing head #14" in r.stdout and "RFC 3161" in r.stdout
    for _ in range(100):
        if any(a["status"] == "complete" for a in anchors_of(qr)):
            break
        time.sleep(0.1)
    assert [a["status"] for a in anchors_of(qr)] == ["pending", "complete"]


def test_session_end_anchors_when_enabled(project, tsa_env):
    harness.init_store(project)
    env = harness.hook_env(project, AUTHORSHIP_ANCHOR="1", **tsa_env["env"])
    harness.run_hook(project, {"hook_event_name": "UserPromptSubmit", "prompt": "#idea x", "cwd": project}, env=env)
    t = time.perf_counter()
    harness.run_hook(project, {"hook_event_name": "SessionEnd", "reason": "other", "cwd": project}, env=env)
    assert time.perf_counter() - t < 1.0
    store = ledger.Store(project)
    for _ in range(100):
        if any(a["status"] == "complete" for a in anchors_of(store)):
            break
        time.sleep(0.1)
    assert anchors_of(store)[-1]["status"] == "complete" and anchors_of(store)[0]["anchored_seq"] == 2


def test_no_tsa_no_ots_anchor_stays_pending(qr, monkeypatch):
    monkeypatch.setenv("AUTHORSHIP_TSA", "off")
    monkeypatch.setenv("AUTHORSHIP_OTS", "0")
    res = anchor.run(qr)
    assert res["methods"] == [] and [a["status"] for a in anchors_of(qr)] == ["pending"]
    assert ledger.verify(qr)["sealed_upto"] == 0


# --- status line ------------------------------------------------------------------


def statusline(project, env=None):
    t = time.perf_counter()
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "statusline.py")],
                       input=json.dumps({"workspace": {"project_dir": project}}), capture_output=True, text=True,
                       env=env or harness.hook_env(project))
    return r.stdout, time.perf_counter() - t


def test_statusline_under_50ms_and_format(qr):
    annotator.run_once(qr)
    out, _ = statusline(qr.project)
    assert re.match(r"^authorship ✓ 14 \| 14 unsealed \| \d+ to review$", out), out
    times = sorted(statusline(qr.project)[1] for _ in range(20))
    print("statusline median %.1f ms" % (times[10] * 1000))
    assert times[10] < 0.050


def test_statusline_shows_broken_chain(qr):
    lines = open(qr.ledger).read().splitlines()
    lines[4] = lines[4].replace('"PostToolUse"', '"PostToolUsf"')
    open(qr.ledger, "w").write("\n".join(lines) + "\n")
    annotator.run_once(qr)
    assert statusline(qr.project)[0] == "authorship ✗ broken at #5"


def test_statusline_empty_outside_initialized_projects(project):
    assert statusline(project)[0] == ""


def test_imprint_check_reads_bytes_not_openssl_text(fake_tsa, tmp_path):
    """openssl's text dump drops trailing 0x20/0x00 bytes of the imprint; the check must not depend on it."""
    import hashlib
    d = os.path.dirname(fake_tsa["env"]["AUTHORSHIP_TSA_CAFILE"])
    misses = 0
    for i in range(150):
        txt = tmp_path / ("a%d.txt" % i)
        txt.write_text("authorship-ledger\nseq: %d\n" % i)
        q, r = str(tmp_path / "q.tsq"), str(tmp_path / "r.tsr")
        subprocess.run(["openssl", "ts", "-query", "-data", str(txt), "-sha256", "-cert", "-out", q], check=True, capture_output=True)
        subprocess.run(["openssl", "ts", "-reply", "-queryfile", q, "-config", "tsa.cnf", "-section", "tsa_config", "-out", r],
                       cwd=d, check=True, capture_output=True)
        want = hashlib.sha256(txt.read_bytes()).hexdigest()
        misses += not anchor.covers(open(r, "rb").read(), want)
        assert not anchor.covers(open(r, "rb").read(), hashlib.sha256(b"other").hexdigest())
    assert misses == 0
