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
    for key in ("nodes", "edges", "entries", "chain", "stages", "claims", "review", "etag", "limits"):
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
    assert g["limits"]["stages_only_above"] == 5000
    status, headers, body = served.request("GET", "/api/graph", headers={"If-None-Match": g["etag"]})
    assert status == 304 and body == b""
    # a new entry changes the etag
    ledger.write_note(served.store, "a later remark")
    status, headers, g2 = served.get_json("/api/graph", headers={"If-None-Match": g["etag"]})
    assert status == 200 and g2["etag"] != g["etag"]


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
    assert status == 200 and b"Authorship ledger" in body
    csp = headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-eval" not in csp
    assert b"<script>" not in body  # no inline scripts (CSP would block them)
    for path in ("/app.js", "/app.css", "/vendor/cytoscape.min.js", "/vendor/elk.bundled.js",
                 "/vendor/cytoscape-elk.js", "/vendor/cytoscape-expand-collapse.js"):
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
    wait_js(page, "() => /chain|broken/.test(document.getElementById('chain-badge').textContent)")
    return ctx, page, problems


def click_tab(page, view):
    page.click("#tab-" + view)
    if view in ("reasoning", "genealogy", "replay"):
        wait_js(page, "() => window.__authorshipPerf.ready && !!window.__authorship.cy()", 30)


def test_browser_smoke_golden(browser, served):
    store = served.store
    ctx, page, problems = open_page(browser, served, "view=stages")
    # the secret moved to sessionStorage and left the URL
    assert "k=" not in page.url and "view=stages" in page.url
    assert page.evaluate("() => sessionStorage.getItem('authorship.viewer.secret')") == served.secret
    assert page.inner_text("#chain-badge").startswith("✓ chain verified")
    assert " %d entries" % len(entries(store)) in page.inner_text("#chain-badge")

    # Stages: one column per stage, dead branch for the discard, full text and diff on expand
    wait_js(page, "() => document.querySelectorAll('.stage-col').length === 2")
    assert page.eval_on_selector_all(".stage-col h2", "els => els.map(e => e.textContent)") == ["Exploration", "Prototype"]
    assert "dead" in page.get_attribute('.card[data-seq="10"]', "class")
    assert "discarded" in page.inner_text('.card[data-seq="10"]')
    page.click('.card[data-seq="3"] button.more')
    wait_js(page, "() => (document.querySelector('.card[data-seq=\"3\"] pre.text') || {}).textContent")
    assert "\n1. Batch per lot" in page.inner_text('.card[data-seq="3"] pre.text')
    page.click('.card[data-seq="7"] button.more')
    wait_js(page, "() => document.querySelectorAll('.card[data-seq=\"7\"] pre.diff .add').length > 0")
    assert page.eval_on_selector_all('.card[data-seq="7"] pre.diff .del', "els => els.length") > 0
    page.click('.card[data-seq="5"] button.more')  # Write: whole file added
    wait_js(page, "() => document.querySelectorAll('.card[data-seq=\"5\"] pre.diff .add').length >= 5")

    # keyboard: arrow keys move between tabs
    page.focus("#tab-stages")
    page.keyboard.press("ArrowRight")
    wait_js(page, "() => document.getElementById('tab-reasoning').getAttribute('aria-selected') === 'true'")
    wait_js(page, "() => window.__authorshipPerf.ready")

    # Reasoning: ELK below the threshold, shapes paired with author, dashed discards and negative edges
    layout = page.evaluate("() => window.__authorshipPerf.layout")
    assert layout["mode"] == "elk" and layout["nodes"] == 15
    info = page.evaluate("""() => { const cy = window.__authorship.cy(); const n = id => cy.getElementById(id);
      const lane = id => n(id).data('lane');
      return { human: n('4').style('shape'), ai: n('3.3').style('shape'), dead: n('10').style('border-style'),
               rejected: n('3.2').style('border-style'), neg: cy.edges('[type = "rejects"]').style('line-style'),
               obj: cy.edges('[type = "objects_to"]').style('line-style'), impl: cy.edges('[type = "implements"]').style('line-style'),
               lanes: [lane('2'), lane('3.3'), lane('6'), lane('11'), lane('13')],
               parent: n('3.3').parent().id(), xs: ['2', '3.3', '4', '11', '13'].map(id => n(id).position('x')),
               laneNodes: cy.nodes('.lane').length, count: cy.nodes().not('.lane').length }; }""")
    assert info["human"] == "ellipse" and info["ai"] == "round-rectangle"
    assert info["dead"] == "dashed" and info["rejected"] == "dashed"
    assert info["neg"] == "dashed" and info["obj"] == "dashed" and info["impl"] == "solid"
    assert info["lanes"] == [0, 1, 2, 3, 3]  # Issues, Positions, Arguments, Decisions
    assert info["parent"] == "3" and info["laneNodes"] == 4 and info["count"] == 15
    assert info["xs"] == sorted(info["xs"]) and info["xs"][0] < info["xs"][-1]  # time on the x axis
    assert page.is_visible("#legend") and "Human" in page.inner_text("#legend") and "AI" in page.inner_text("#legend")
    # expand-collapse folds a response's options into the response node
    page.click("#btn-collapse")
    assert page.evaluate("() => window.__authorship.cy().getElementById('3.1').length") == 0
    page.click("#btn-expand")
    assert page.evaluate("() => window.__authorship.cy().getElementById('3.1').length") == 1
    # selecting a node shows its entry
    page.evaluate("() => window.__authorship.cy().getElementById('4').emit('tap')")
    wait_js(page, "() => document.getElementById('detail').textContent.indexOf('#4') >= 0")
    wait_js(page, "() => document.getElementById('detail').textContent.indexOf('instead of TTL') >= 0")
    assert "sel=4" in page.url

    # Genealogy: claim 13, confirmed edges only
    click_tab(page, "genealogy")
    page.select_option("#gen-claim", "13")
    if not page.is_checked("#gen-confirmed"):
        page.check("#gen-confirmed")
    wait_js(page, "() => document.getElementById('gen-lineage')")
    assert page.get_attribute("#gen-lineage", "data-lineage") == "2,3.3,4,11,13"
    ai_item = page.inner_text('#gen-ai li[data-node="3.3"]')
    assert "AI-originated, modified by the human at #4" in ai_item
    assert page.eval_on_selector_all("#gen-ai li", "els => els.length") == 1
    summary = page.inner_text("#gen-summary")
    assert "Human-originated ancestors: 3" in summary and "AI-originated ancestors: 1" in summary
    assert "claim=13" in page.url and "conf=1" in page.url
    dim = page.evaluate("""() => { const cy = window.__authorship.cy();
      return { lin: ['2','3.3','4','11','13'].every(id => cy.getElementById(id).hasClass('lin')),
               dimmed: ['3.1','3.2','5','9','10','12'].every(id => cy.getElementById(id).hasClass('dim')) }; }""")
    assert dim == {"lin": True, "dimmed": True}
    # all edges: the lineage also runs through the rule edges (responses)
    page.uncheck("#gen-confirmed")
    wait_js(page, "() => document.getElementById('gen-lineage').dataset.lineage.split(',').includes('9')")

    # Replay: the slider hides everything after the chosen seq, in every graph view
    click_tab(page, "replay")
    page.evaluate("""() => { const s = document.getElementById('rp-slider'); const m = window.__authorship.model();
      s.value = String(m.seqs.indexOf(4)); s.dispatchEvent(new Event('input')); }""")
    wait_js(page, "() => window.__authorship.state.seq === 4")
    vis = page.evaluate("""() => { const cy = window.__authorship.cy();
      return { four: cy.getElementById('4').visible(), opt: cy.getElementById('3.3').visible(),
               eleven: cy.getElementById('11').visible(), five: cy.getElementById('5').visible() }; }""")
    assert vis == {"four": True, "opt": True, "eleven": False, "five": False}
    assert "seq=4" in page.url
    for sub in ("genealogy", "branches"):
        page.select_option("#replay-view", sub)
        time.sleep(0.2)
    rows = page.evaluate("() => window.__authorshipBranches.nodes.map(n => n.id)")
    assert rows == ["2", "3", "3.1", "3.2", "3.3", "4"]
    page.click("#rp-next")
    wait_js(page, "() => window.__authorship.state.seq === 5")
    page.click("#rp-live")
    wait_js(page, "() => window.__authorship.state.seq === null")
    page.select_option("#replay-view", "reasoning")

    # Branches: approaches as branches, adoption as merge, discards as dead ends with the reason
    click_tab(page, "branches")
    wait_js(page, "() => document.querySelectorAll('#branches .row').length === 15")
    br = page.evaluate("""() => { const B = window.__authorshipBranches; const o = {};
      B.branches.forEach(b => { o[b.root] = { kind: b.endKind, end: b.end, note: b.note, col: b.col }; }); return o; }""")
    assert br["3.2"]["kind"] == "dead" and br["3.2"]["end"] == "11" and "rejected at #11" in br["3.2"]["note"]
    assert br["10"]["kind"] == "dead"
    assert br["4"]["kind"] == "merge" and br["4"]["end"] == "11"
    assert br["3.3"]["kind"] == "modified" and br["3.3"]["col"] == br["4"]["col"]
    assert br["3.1"]["kind"] == "open"
    svg_text = page.inner_text("#branches")
    assert "✗ rejected at #11" in svg_text and "adopted at #11" in svg_text
    page.click('#branches .row[data-id="11"]')
    wait_js(page, "() => document.getElementById('detail').textContent.indexOf('#11') >= 0")

    # Review: accept one item through the UI; the returned #seq is shown and a Confirm entry exists
    click_tab(page, "review")
    wait_js(page, "() => document.querySelectorAll('#review li[data-key]').length === 2")
    assert page.inner_text("#review-count") == "2"
    n_before = len(entries(store))
    item = '#review li[data-key="4|milestone:conception_candidate"]'
    page.click(item + " button.accept")
    wait_js(page, "() => document.getElementById('review-recorded').textContent.indexOf('Recorded as #') >= 0")
    expected = n_before + 1
    assert "Recorded as #%d" % expected in page.inner_text("#review-recorded")
    last = entries(store)[-1]
    assert last["seq"] == expected and last["event"] == "Confirm" and last["actor"] == "human"
    assert last["decision"] == "accept" and last["label"] == "milestone:conception_candidate" and last["target_seq"] == 4
    assert last["annotation_id"] and last["annotation_id"].startswith("ann_")
    # the queue refreshes: one item left; edit it into another relation
    wait_js(page, "() => document.querySelectorAll('#review li[data-key]').length === 1")
    edge_item = '#review li[data-key="4|edge:4:rejects:3.1"]'
    page.click(edge_item + " button.edit")
    page.fill(edge_item + " .edit-row input", "edge:4:objects_to:3.1")
    page.click(edge_item + " button.save")
    wait_js(page, "() => document.getElementById('review-recorded').textContent.indexOf('Recorded as #%d') >= 0" % (expected + 1))
    last = entries(store)[-1]
    assert last["decision"] == "edit" and last["label"] == "edge:4:rejects:3.1" and last["edited_label"] == "edge:4:objects_to:3.1"
    assert ledger.verify(store)["ok"]
    wait_js(page, "() => document.querySelector('#review li.empty')")
    wait_js(page, "() => document.querySelector('#confirms li[data-seq=\"%d\"]')" % (expected + 1))
    wait_js(page, "() => /%d entries/.test(document.getElementById('chain-badge').textContent)" % (expected + 1))

    # no horizontal page scroll at a narrow width; graphs pan inside their container
    page.set_viewport_size({"width": 380, "height": 800})
    for view in ("stages", "reasoning", "branches", "review"):
        click_tab(page, view)
        time.sleep(0.2)
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1"), view
    assert problems == []
    ctx.close()


def test_browser_without_secret_is_read_only(browser, served):
    ctx, page, problems = open_page(browser, served, "view=review", with_secret=False)
    wait_js(page, "() => document.querySelectorAll('#review li[data-key]').length === 2")
    assert page.is_visible("#readonly-hint")
    assert "viewer.py open" in page.inner_text("#readonly-hint")
    assert page.eval_on_selector_all("#review button.accept, #review button.reject, #review button.edit",
                                     "els => els.every(b => b.disabled)")
    n = len(entries(served.store))
    page.evaluate("() => document.querySelector('#review button.accept').click()")
    time.sleep(0.3)
    assert len(entries(served.store)) == n
    # a forged request from the page without the secret is refused by the server
    status = page.evaluate("""() => fetch('/api/confirm', {method: 'POST', referrerPolicy: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({target_seq: 4, decision: 'accept', label: 'x'})})
      .then(r => r.status)""")
    assert status == 403 and len(entries(served.store)) == n
    assert problems == [] or all("403" in p for p in problems)
    ctx.close()


def test_browser_badge_shows_broken_chain(browser, served):
    ctx, page, problems = open_page(browser, served, "view=stages")
    assert page.inner_text("#chain-badge").startswith("✓")
    tamper(served.store, 11, "webhook", "webhoox")
    # the page notices on its next poll (4 s): the etag changes even though the head did not
    wait_js(page, "() => /broken at #11/.test(document.getElementById('chain-badge').textContent)", timeout=15)
    assert "broken" in page.get_attribute("#chain-badge", "class")
    assert page.is_visible("#broken-banner") and "broken at #11" in page.inner_text("#broken-banner")
    ctx.close()
    # and a fresh load shows it straight away
    ctx, page, _ = open_page(browser, served, "view=reasoning")
    assert page.inner_text("#chain-badge") == "✗ broken at #11"
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


def test_browser_5000_nodes_budgets_and_5001_fallback(browser, project):
    store = synthetic_ledger(harness.init_store(project), 5000)
    s = Served(store)
    try:
        ctx, page, problems = open_page(browser, s, "view=reasoning")
        wait_js(page, "() => window.__authorshipPerf.ready", timeout=30)
        layout = page.evaluate("() => window.__authorshipPerf.layout")
        print("\n5000-node layout: %r" % layout)
        assert layout["nodes"] == 5000 and layout["mode"] == "preset"
        assert layout["totalMs"] < 3000, layout
        assert page.evaluate("() => window.__authorship.cy().nodes().not('.lane').length") == 5000
        pan = page.evaluate("() => window.__authorshipPerf.panTest(1000)")
        print("5000-node pan at zoom %.2f: %.1f fps (worst frame %d ms)" % (pan["zoom"], pan["fps"], pan["worstFrameMs"]))
        assert pan["fps"] >= 30, pan
        # every other view loads too
        click_tab(page, "genealogy")
        wait_js(page, "() => document.getElementById('gen-lineage')")
        t0 = time.time()
        click_tab(page, "branches")
        wait_js(page, "() => document.querySelectorAll('#branches .row').length === 5000", timeout=20)
        branches_s = time.time() - t0
        print("5000-node branches render: %.2f s" % branches_s)
        assert branches_s < 5
        click_tab(page, "replay")
        page.evaluate("""() => { const s = document.getElementById('rp-slider'); s.value = '100';
          s.dispatchEvent(new Event('input')); }""")
        wait_js(page, "() => window.__authorship.state.seq !== null")
        hidden = page.evaluate("() => window.__authorship.cy().nodes('.future').length")
        assert hidden > 4000
        click_tab(page, "stages")
        wait_js(page, "() => document.querySelectorAll('.stage-col').length === 5")
        click_tab(page, "review")
        assert problems == []
        ctx.close()

        # one more node: above graph.limits.stages_only_above, only Stages is offered
        ledger.write_note(store, "one more remark")
        assert len(viewer.build_graph(store)["nodes"]) == 5001
        ctx, page, problems = open_page(browser, s, "view=reasoning")
        wait_js(page, "() => !document.getElementById('notice').hidden")
        assert "Showing the Stages view only" in page.inner_text("#notice")
        assert "5,001 nodes" in page.inner_text("#notice")
        assert page.get_attribute("#tab-stages", "aria-selected") == "true"
        assert page.is_visible("#panel-stages") and not page.is_visible("#panel-graph")
        for v in ("reasoning", "genealogy", "branches", "replay"):
            assert page.is_disabled("#tab-" + v)
        assert page.is_enabled("#tab-review")
        wait_js(page, "() => document.querySelectorAll('.stage-col').length === 5")
        assert page.evaluate("() => window.__authorship.cy()") is None
        assert problems == []
        ctx.close()
    finally:
        s.close()
