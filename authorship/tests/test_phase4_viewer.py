"""Phase 4: viewer API, confirmation write path, and a headless-browser smoke test of every view.

The browser tests need Playwright (pip install playwright) and a Chromium build; they are skipped
when either is missing. They reuse a cached Playwright Chromium or the installed Google Chrome.
"""
import glob
import http.client
import json
import os
import threading
import time
import uuid

import pytest

import annotator
import harness
import ledger
import viewer
from harness import entries

# ---------------------------------------------------------------------------------------------
# helpers


@pytest.fixture
def golden(qr_confirmed):
    """Golden fixture with confirmed edges, Tier 0 milestones and one pending Tier 1 annotation on #4
    (a conception candidate at 0.83 and an edge suggestion), so the Review queue has two items."""
    store = qr_confirmed
    annotator.run_once(store)
    write_tier1(store, 4, milestones=[{"type": "conception_candidate", "tier": 1, "score": 0.83, "band": "high"}],
                edges=[{"src": "4", "dst": "3.1", "type": "rejects", "p": 0.62}])
    return store


def write_tier1(store, target, milestones, edges):
    e = ledger.find_entry(store, target)
    ann = {"id": "ann_" + uuid.uuid4().hex, "ts": ledger.now_iso(), "target_seq": target, "target_hash": e["hash"],
           "model": "jev-1.13.0", "questions_hash": "0" * 64,
           "answers": {"stance": {"choice": "modifies", "probabilities": {"modifies": 0.83, "originates": 0.1}}},
           "milestones": milestones, "edges": edges, "supersedes": None}
    with open(store.annotations, "a", encoding="utf-8") as f:
        f.write(json.dumps(ann) + "\n")
    return ann


class Served(object):
    def __init__(self, store):
        self.store = store
        self.server = viewer.start(store, port=0, open_browser=False)
        self.port = self.server.server_address[1]
        self.secret = self.server.secret
        self.origin = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or "127.0.0.1:%d" % self.port)
        data = body.encode("utf-8") if isinstance(body, str) else body
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        r = conn.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        conn.close()
        return out

    def get_json(self, path, **kw):
        status, headers, body = self.request("GET", path, **kw)
        return status, headers, (json.loads(body.decode("utf-8")) if body else None)

    def confirm(self, payload, secret=True, origin=True, ctype="application/json"):
        headers = {}
        if ctype:
            headers["Content-Type"] = ctype
        if secret:
            headers["X-Authorship-Secret"] = self.secret if secret is True else secret
        if origin:
            headers["Origin"] = self.origin if origin is True else origin
        return self.request("POST", "/api/confirm", body=json.dumps(payload), headers=headers)


@pytest.fixture
def served(golden):
    s = Served(golden)
    yield s
    s.close()


def confirms(store):
    return [e for e in entries(store) if e["event"] == "Confirm"]


def tamper(store, seq, old, new):
    """Change bytes inside one entry line of ledger.jsonl, keeping the line valid JSON and the same size."""
    assert len(old) == len(new)
    with open(store.ledger, "r", encoding="utf-8") as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        if json.loads(line)["seq"] == seq:
            assert old in line
            lines[i] = line.replace(old, new, 1)
            break
    with open(store.ledger, "w", encoding="utf-8") as f:
        f.writelines(lines)


# ---------------------------------------------------------------------------------------------
# API


def test_graph_shape_and_etag(served):
    status, headers, g = served.get_json("/api/graph")
    assert status == 200
    for key in ("nodes", "edges", "entries", "chain", "stages", "claims", "review", "etag", "signatures"):
        assert key in g, key
    assert headers["ETag"] == g["etag"]
    assert g["chain"]["ok"] is True and g["chain"]["entries"] == len(entries(served.store))
    assert [s["name"] for s in g["stages"]] == ["Exploration", "Prototype"]
    assert g["claims"] == ["13"]
    ids = {n["id"] for n in g["nodes"]}
    assert {"2", "3", "3.1", "3.2", "3.3", "4", "10", "11", "13"} <= ids
    assert {"src": "4", "dst": "3.3", "type": "modifies", "source": "confirmed"} in g["edges"]
    labels = {(i["target_seq"], i["label"]) for i in g["review"]}
    assert (4, "milestone:conception_candidate") in labels and (4, "edge:4:rejects:3.1") in labels
    assert g["signatures"] == {"human": sum(1 for e in entries(served.store) if e["actor"] == "human"), "signed": 0}
    status, headers, body = served.request("GET", "/api/graph", headers={"If-None-Match": g["etag"]})
    assert status == 304 and body == b""
    # a new entry changes the etag
    ledger.write_note(served.store, "a later remark")
    status, headers, g2 = served.get_json("/api/graph", headers={"If-None-Match": g["etag"]})
    assert status == 200 and g2["etag"] != g["etag"]


def test_large_responses_are_gzipped_when_accepted(served):
    import gzip
    status, headers, body = served.request("GET", "/api/graph", headers={"Accept-Encoding": "gzip, deflate"})
    assert status == 200 and headers.get("Content-Encoding") == "gzip" and headers.get("Vary") == "Accept-Encoding"
    assert json.loads(gzip.decompress(body))["chain"]["ok"]
    status, headers, body = served.request("GET", "/api/graph")  # no Accept-Encoding: plain
    assert "Content-Encoding" not in headers and json.loads(body)["chain"]["ok"]
    status, headers, _ = served.request("GET", "/app.css", headers={"Accept-Encoding": "gzip"})
    assert "Content-Encoding" not in headers  # small files stay plain


def test_foreign_host_is_rejected(served):
    for host in ("evil.example:%d" % served.port, "127.0.0.1:1", "attacker.localhost"):
        status, _, _ = served.request("GET", "/api/graph", host=host)
        assert status == 403
        status, _, _ = served.request("POST", "/api/confirm", body="{}", host=host,
                                      headers={"Content-Type": "application/json", "X-Authorship-Secret": served.secret,
                                               "Origin": served.origin})
        assert status == 403
    status, _, _ = served.request("GET", "/", host="localhost:%d" % served.port)
    assert status == 200


def test_confirm_requires_secret_origin_and_json(served):
    store = served.store
    n_before = len(entries(store))
    payload = {"target_seq": 4, "annotation_id": None, "decision": "accept", "label": "milestone:conception_candidate",
               "edited_label": None}
    assert served.confirm(payload, secret=False)[0] == 403
    assert served.confirm(payload, secret="wrong-secret")[0] == 403
    assert served.confirm(payload, origin=False)[0] == 403
    assert served.confirm(payload, origin="http://evil.example")[0] == 403
    assert served.confirm(payload, origin="null")[0] == 403
    assert served.confirm(payload, ctype=None)[0] == 403
    assert served.confirm(payload, ctype="text/plain")[0] == 403
    assert served.confirm(payload, ctype="application/x-www-form-urlencoded")[0] == 403
    assert len(entries(store)) == n_before  # nothing was written
    status, _, body = served.confirm(payload)
    assert status == 200
    res = json.loads(body)
    last = entries(store)[-1]
    assert res["seq"] == last["seq"] == n_before + 1 and res["hash"] == last["hash"][:12]
    assert last["event"] == "Confirm" and last["actor"] == "human"
    assert last["target_seq"] == 4 and last["target_hash"] == ledger.find_entry(store, 4)["hash"]
    assert last["decision"] == "accept" and last["label"] == "milestone:conception_candidate"
    assert ledger.verify(store)["ok"]
    # the item leaves the review queue
    _, _, g = served.get_json("/api/graph")
    assert (4, "milestone:conception_candidate") not in {(i["target_seq"], i["label"]) for i in g["review"]}
    # bad bodies are 400, not writes
    assert served.confirm({"target_seq": 999, "decision": "accept", "label": "x"})[0] == 400
    assert served.confirm({"target_seq": 4, "decision": "maybe", "label": "x"})[0] == 400
    assert served.confirm({"target_seq": 4, "decision": "edit", "label": "x"})[0] == 400
    assert len(entries(store)) == n_before + 1


def test_static_files_and_traversal(served):
    status, headers, body = served.request("GET", "/")
    assert status == 200 and b"Authorship record" in body
    csp = headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-eval" not in csp
    assert b"<script>" not in body  # no inline scripts (CSP would block them)
    for path in ("/app.js", "/app.css"):
        assert served.request("GET", path)[0] == 200, path
    for path in ("/../scripts/ledger.py", "/vendor/../../scripts/ledger.py", "/..%2fscripts/ledger.py",
                 "/%2e%2e/scripts/ledger.py", "/vendor/../../tests/conftest.py", "//etc/passwd", "/nope.html"):
        assert served.request("GET", path)[0] == 404, path


def test_blob_entry_and_report_endpoints(served):
    assert served.request("GET", "/api/blob/not-a-sha")[0] == 400
    assert served.request("GET", "/api/blob/" + "g" * 64)[0] == 400
    assert served.request("GET", "/api/blob/" + "0" * 64)[0] == 404
    e5 = ledger.find_entry(served.store, 5)
    status, _, body = served.request("GET", "/api/blob/" + e5["input"]["blob"])
    assert status == 200 and json.loads(body)["file_path"].endswith("src/recon.py")
    assert served.request("GET", "/api/entry/abc")[0] == 400
    assert served.request("GET", "/api/entry/999")[0] == 404
    status, _, j = served.get_json("/api/entry/3")
    assert status == 200 and "\n1. Batch per lot" in j["text"]  # full text, line breaks kept
    status, headers, body = served.request("GET", "/api/report.md")
    assert status == 200 and "Draft for attorney review" in body.decode("utf-8")
    assert headers["Content-Type"].startswith("text/markdown")


def test_tampering_shows_in_graph_chain_and_etag(served):
    _, _, g = served.get_json("/api/graph")
    assert g["chain"]["ok"]
    tamper(served.store, 4, "TTL", "TTX")  # same size: only the etag's mtime notices
    status, _, g2 = served.get_json("/api/graph", headers={"If-None-Match": g["etag"]})
    assert status == 200, "an in-place edit must change the etag, so polling pages refetch"
    assert g2["chain"]["ok"] is False and g2["chain"]["broken_at"] == 4
    assert g2["chain"]["reason"]


# ---------------------------------------------------------------------------------------------
# Headless browser


CACHE = os.path.expanduser(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "~/Library/Caches/ms-playwright")
if not os.path.isdir(CACHE):
    CACHE = os.path.expanduser("~/.cache/ms-playwright")


def _launch(p):
    errors = []
    try:
        return p.chromium.launch()
    except Exception as exc:  # the pinned build is not downloaded: try cached builds, then Chrome
        errors.append(exc)
    candidates = sorted(glob.glob(os.path.join(CACHE, "chromium_headless_shell-*", "chrome-headless-shell-*",
                                               "chrome-headless-shell")), reverse=True)
    candidates += sorted(glob.glob(os.path.join(CACHE, "chromium-*", "chrome-*", "Chromium.app", "Contents", "MacOS",
                                                "Chromium")), reverse=True)
    candidates += sorted(glob.glob(os.path.join(CACHE, "chromium-*", "chrome-linux*", "chrome")), reverse=True)
    for exe in candidates:
        try:
            return p.chromium.launch(executable_path=exe)
        except Exception as exc:
            errors.append(exc)
    try:
        return p.chromium.launch(channel="chrome")
    except Exception as exc:
        errors.append(exc)
    pytest.skip("no usable Chromium for Playwright: %s" % errors[-1])



@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        b = _launch(p)
        yield b
        b.close()


def wait_js(page, fn, timeout=30.0):
    """Poll a JS function from Python (page.wait_for_function would need 'unsafe-eval', which the CSP forbids)."""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        last = page.evaluate(fn)
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError("timed out after %.0fs waiting for %s (last=%r)" % (timeout, fn, last))


def open_page(browser, served, hash_="", with_secret=True, viewport=None):
    ctx = browser.new_context(viewport=viewport or {"width": 1400, "height": 900})
    page = ctx.new_page()
    problems = []
    page.on("pageerror", lambda e: problems.append("pageerror: %s" % e))
    page.on("console", lambda m: problems.append("console.%s: %s" % (m.type, m.text)) if m.type == "error" else None)
    frag = []
    if with_secret:
        frag.append("k=" + served.secret)
    if hash_:
        frag.append(hash_)
    page.goto(served.origin + "/" + ("#" + "&".join(frag) if frag else ""))
    wait_js(page, "() => /Record/.test(document.getElementById('chain-badge').textContent)")
    return ctx, page, problems


def click_tab(page, view):
    page.click("#tab-" + view)
    wait_js(page, "() => window.__authorshipPerf.view === '%s'" % view)


def test_browser_overview_answers_who_contributed_what(browser, served):
    ctx, page, problems = open_page(browser, served)
    # the secret moved to sessionStorage and left the URL
    assert "k=" not in page.url
    assert page.evaluate("() => sessionStorage.getItem('authorship.viewer.secret')") == served.secret
    badge = page.inner_text("#chain-badge")
    assert badge.startswith("✓ Record intact") and "not sealed yet" in badge and "signed" not in badge
    wait_js(page, "() => document.querySelector('.claim[data-claim=\"13\"]')")
    assert page.inner_text("#review-count") == "2"
    assert not page.is_visible("#cls-strip")  # the default classifier needs no warning
    claim = page.inner_text('.claim[data-claim="13"]')
    assert "method that detects the absence of new transactions" in claim
    assert "From you 2" in claim.replace("\n", " ") and "From Claude 1" in claim.replace("\n", " ")
    assert "changes Claude’s #3.3" in claim and "tests prove it at #8" in claim and "changed by you at #4" in claim
    assert "TTL cache" in page.inner_text("#against")  # what came from Claude, stated plainly
    assert "Exploration" in page.inner_text("#stages-summary") and "Prototype" in page.inner_text("#stages-summary")
    # an element's #N opens that entry in the Timeline
    page.click('.claim[data-claim="13"] a:has-text("#4")')
    wait_js(page, "() => document.querySelector('.tl-item[data-seq=\"4\"] .tl-detail')")
    assert "view=timeline" in page.url and "open=4" in page.url
    assert "changes #3.3" in page.inner_text('.tl-item[data-seq="4"] .tl-detail')
    # the help dialog explains the three views and says who labels the entries
    page.click("#help-btn")
    help_text = page.inner_text("#help")
    assert page.is_visible("#help") and "Timeline" in help_text and "Map" not in help_text
    page.keyboard.press("Escape")
    assert problems == []
    ctx.close()


def test_browser_timeline_tells_the_story(browser, served):
    ctx, page, problems = open_page(browser, served, "view=timeline")
    wait_js(page, "() => document.querySelectorAll('.tl-stage').length >= 2")
    heads = page.eval_on_selector_all(".tl-stage-head h2", "els => els.map(e => e.textContent)")
    assert heads[:2] == ["Exploration", "Prototype"]
    text = page.inner_text("#timeline")
    assert "stated the problem" in text and "offered 3 options" in text and "proposed an idea, changing Claude’s #3.3" in text
    assert "they pass after failing" in text and "Evidence that idea #4 works" in text
    assert "rejected" in page.inner_text('.tl-item[data-seq="3"]')
    assert "dead" in page.get_attribute('.tl-item[data-seq="10"]', "class")
    assert not page.query_selector('.tl-item[data-seq="7"]')  # routine work is folded in Key moments
    # open #4: full text and its links
    page.click('.tl-item[data-seq="4"] .tl-row')
    wait_js(page, "() => document.querySelector('.tl-item[data-seq=\"4\"] .tl-detail pre.text')")
    det = page.inner_text('.tl-item[data-seq="4"] .tl-detail')
    assert "changes #3.3" in det and "is shown working by #8" in det
    # Everything: every step, with the code change
    page.click('#tl-filter button[data-f="all"]')
    wait_js(page, "() => document.querySelector('.tl-item[data-seq=\"7\"]')")
    page.click('.tl-item[data-seq="7"] .tl-row')
    wait_js(page, "() => document.querySelectorAll('.tl-item[data-seq=\"7\"] pre.diff .add').length > 0")
    assert page.eval_on_selector_all('.tl-item[data-seq="7"] pre.diff .del', "els => els.length") > 0
    # search
    page.fill("#tl-search", "bloom")
    wait_js(page, "() => document.querySelectorAll('.tl-item').length === 1")
    assert "tl=all" in page.url and "q=bloom" in page.url
    assert problems == []
    ctx.close()


def test_browser_review_records_answers(browser, served):
    store = served.store
    ctx, page, problems = open_page(browser, served, "view=review")
    wait_js(page, "() => document.querySelectorAll('#review-pending .q-card').length === 2")
    assert page.inner_text("#review-count") == "2"
    qs = page.inner_text("#review-pending")
    assert "Is #4 a conception moment: you introduced a new technical element?" in qs and "classifier 83% sure" in qs
    assert "Is it right that #4 rejects #3.1?" in qs
    n = len(entries(store))
    page.click('#review-pending .q-card[data-label="milestone:conception_candidate"] button.yes')
    wait_js(page, "() => /Recorded as #/.test(document.getElementById('review-pending').textContent) || document.querySelectorAll('#review-pending .q-card').length === 1")
    wait_js(page, "() => document.querySelectorAll('#review-pending .q-card').length === 1", timeout=15)
    e = entries(store)[n]
    assert (e["event"], e["actor"], e["decision"], e["label"], e["target_seq"]) == ("Confirm", "human", "accept", "milestone:conception_candidate", 4)
    # change the edge suggestion to another relation
    card = '#review-pending .q-card[data-label="edge:4:rejects:3.1"]'
    page.click(card + " button:has-text('Change')")
    page.select_option(card + " select", "edge:4:modifies:3.1")
    page.click(card + " button:has-text('Save change')")
    wait_js(page, "() => document.querySelectorAll('#review-pending .q-card').length === 0", timeout=15)
    last = entries(store)[-1]
    assert last["decision"] == "edit" and last["edited_label"] == "edge:4:modifies:3.1"
    done = len(confirms(store))
    wait_js(page, "() => document.querySelectorAll('#review-done .done-row').length === %d" % min(done, 50))
    assert "Changed" in page.inner_text("#review-done .done-row") and "#4 rejects #3.1 \u2192 #4 changes #3.1" in page.inner_text("#review-done .done-row")
    assert ledger.verify(store)["ok"]
    # keyboard: arrow keys move between tabs
    page.focus("#tab-review")
    page.keyboard.press("ArrowLeft")
    wait_js(page, "() => document.getElementById('tab-timeline').getAttribute('aria-selected') === 'true'")
    assert problems == []
    ctx.close()


def test_browser_without_secret_is_read_only(browser, served):
    ctx, page, problems = open_page(browser, served, "view=review", with_secret=False)
    wait_js(page, "() => document.querySelectorAll('#review-pending .q-card').length === 2")
    assert page.is_visible("#review-locked") and "authorship open" in page.inner_text("#review-locked")
    assert page.eval_on_selector_all("#review-pending button", "els => els.every(b => b.disabled)")
    n = len(entries(served.store))
    status = page.evaluate("""() => fetch('/api/confirm', {method: 'POST', referrerPolicy: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({target_seq: 4, decision: 'accept', label: 'x'})})
      .then(r => r.status)""")
    assert status == 403 and len(entries(served.store)) == n
    assert problems == [] or all("403" in p for p in problems)
    ctx.close()


def test_browser_badge_shows_broken_chain(browser, served):
    ctx, page, problems = open_page(browser, served)
    assert page.inner_text("#chain-badge").startswith("✓")
    tamper(served.store, 11, "webhook", "webhoox")
    # the page notices on its next poll (4 s): the etag changes even though the head did not
    wait_js(page, "() => /broken at #11/.test(document.getElementById('chain-badge').textContent)", timeout=15)
    assert "bad" in page.get_attribute("#chain-badge", "class")
    assert page.is_visible("#broken-banner") and "#11 was changed" in page.inner_text("#broken-banner")
    ctx.close()


def test_browser_phone_width_has_no_page_scroll(browser, served):
    ctx, page, problems = open_page(browser, served, viewport={"width": 380, "height": 800})
    for view in ("overview", "timeline", "review"):
        click_tab(page, view)
        time.sleep(0.2)
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1"), view
    assert problems == []
    ctx.close()


# ---------------------------------------------------------------------------------------------
# Scale: a synthetic 5,000-node ledger

STAGES = ["Discovery", "Design", "Prototype", "Hardening", "Rollout"]
TOPICS = ["statement counter", "settlement lot", "webhook signature", "retry budget", "idempotency key",
          "ledger snapshot", "bank cursor", "reconciliation window", "payment batch", "audit trail"]


def synthetic_ledger(store, n_nodes, session="synth-s1"):
    """A realistic mix, written straight through ledger.append: tagged prompts across five stages, AI responses
    with numbered options, file edits, failing and passing test runs, discards, decisions and claims.
    Produces exactly n_nodes index nodes (a response with k options counts 1 + k)."""
    store.ensure()
    ledger.append(store, "SessionStart", "system", session, {"source": "startup", "model": "synthetic"})

    def prompt(text, note=False):
        tags, stage = ledger.parse_tags(text)
        f = {"kind": "note" if note else "prompt", "tags": tags}
        if note:
            f["author"] = "synthetic"
        if stage:
            f["stage"] = stage
        f.update(ledger.text_fields(store, text))
        ledger.append(store, "ManualNote" if note else "UserPromptSubmit", "human", None if note else session, f)
        return 1

    def response(text, n_opts):
        f = {"kind": "response", "n_blocks": 1}
        f.update(ledger.text_fields(store, text))
        ledger.append(store, "Stop", "ai", session, f)
        return 1 + n_opts

    def tool(name, inp, resp, outcome="success", **extra):
        f = {"kind": "tool", "tool": name, "outcome": outcome, "input": ledger.blob_ref(store, inp),
             "response": ledger.blob_ref(store, resp)}
        f.update(extra)
        ledger.append(store, "PostToolUseFailure" if outcome == "failure" else "PostToolUse", "ai", session, f)
        return 1

    count, turn, stage_i = 0, 0, -1
    per_stage = max(1, n_nodes // len(STAGES))
    while count < n_nodes:
        left, t, k = n_nodes - count, TOPICS[turn % len(TOPICS)], turn % 7
        if count // per_stage > stage_i and stage_i < len(STAGES) - 1:
            stage_i += 1
            count += prompt("#stage %s #problem turn %d: the %s is too slow under load" % (STAGES[stage_i], turn, t))
        elif left < 6:
            count += prompt("turn %d: continue with the %s" % (turn, t))
        elif k == 0:
            count += prompt("#idea turn %d: derive the %s from the monotonic sequence instead of polling" % (turn, t))
        elif k == 1:
            count += response("Options for the %s:\n\n1. Cache it per lot.\n2. Recompute on every call.\n"
                              "3. Push it from the bank.\n\nWhich one?" % t, 3)
        elif k == 2:
            path = "src/mod_%d.py" % (turn % 40)
            count += tool("Edit", {"file_path": path, "old_string": "def f():\n    return 1\n",
                                   "new_string": "def f():\n    return %d\n" % turn}, {"filePath": path}, file=path)
        elif k == 3:
            cmd = "pytest -q tests/test_%d.py" % (turn % 40)
            count += tool("Bash", {"command": cmd}, {"stdout": "1 failed", "stderr": ""}, outcome="failure",
                          command=cmd, error="1 failed")
        elif k == 4:
            cmd = "pytest -q tests/test_%d.py" % (turn % 40)
            count += tool("Bash", {"command": cmd}, {"stdout": "3 passed", "stderr": ""}, command=cmd)
        elif k == 5:
            if turn % 3 == 0:
                count += prompt("#discard turn %d: sharding the %s loses ordering" % (turn, t), note=True)
            else:
                count += prompt("#decision turn %d: keep option 1 for the %s because it is cheaper" % (turn, t))
        elif turn % 11 == 0:
            count += prompt("#claim turn %d: method that checks the %s without downloading the detail" % (turn, t))
        else:
            count += response("Done: the %s now uses the sequence number. Tests pass." % t, 0)
        turn += 1
    assert count == n_nodes
    return store


def test_synthetic_generator_counts(project):
    store = synthetic_ledger(harness.init_store(project), 300)
    g = viewer.build_graph(store)
    assert len(g["nodes"]) == 300 and g["chain"]["ok"]
    assert {n["ibis"] for n in g["nodes"]} >= {"issue", "position", "response", "action", "argument", "decision", "claim"}
    assert [s["name"] for s in g["stages"]] == STAGES


def test_browser_5000_nodes_budgets(browser, project):
    store = synthetic_ledger(harness.init_store(project), 5000)
    s = Served(store)
    try:
        ctx, page, problems = open_page(browser, s)
        t0 = time.time()
        click_tab(page, "timeline")
        wait_js(page, "() => document.querySelectorAll('.tl-item').length >= 300 && !!document.querySelector('.more-row')", timeout=20)
        print("5000-node timeline: %.2f s" % (time.time() - t0))
        assert time.time() - t0 < 5
        click_tab(page, "overview")
        wait_js(page, "() => document.querySelectorAll('.claim').length > 0")
        click_tab(page, "review")
        assert problems == []
        ctx.close()
    finally:
        s.close()


def test_browser_says_who_labels_and_how_to_change_it(browser, served):
    import classifier
    import index
    conn = index.update(served.store)
    info = dict(classifier.describe_backend(classifier.JevBackend(provider=jev_client_fake())), state="on")
    index.store_status(conn, ledger.verify(served.store), 0, classifier=info)
    conn.close()
    ctx, page, problems = open_page(browser, served)
    wait_js(page, "() => /Jev/.test(document.getElementById('cls-strip').textContent)")
    strip = page.inner_text("#cls-strip")
    assert "Labeled automatically by Jev" in strip and "TypeSafe AI" in strip
    assert "third" in page.get_attribute("#cls-strip", "class") and page.is_visible("#cls-strip")
    page.click("#cls-strip button:has-text('How to change it')")
    assert page.is_visible("#setup")
    setup = page.inner_text("#setup")
    assert "export TYPESAFE_API_KEY" in setup and "authorship restart" in setup and "authorship classifier --test" in setup
    assert "AUTHORSHIP_AUTO=0" in setup and "destroy novelty" in setup
    page.keyboard.press("Escape")
    # the default: Claude, no new party
    conn = index.update(served.store)
    index.store_status(conn, ledger.verify(served.store), 0, classifier=dict(
        classifier.describe_backend(classifier.ClaudeCLI()), state="on"))
    conn.close()
    page.reload()
    wait_js(page, "() => /Claude/.test(document.getElementById('help-classifier').textContent)")
    assert not page.is_visible("#cls-strip")  # nothing to warn about
    page.click("#help-btn")
    assert "No new party receives your text" in page.inner_text("#help-classifier")
    page.click("#setup-btn")
    assert page.is_visible("#setup") and not page.is_visible("#help")
    page.keyboard.press("Escape")
    assert problems == []
    ctx.close()


def jev_client_fake():
    import jev_client
    return jev_client.TypesafeProvider(key="k")
