#!/usr/bin/env python3
"""Local, read-only viewer for the authorship ledger. Confirmations are its only write.

    viewer.py daemon [--project DIR]      start (SessionStart does this), open the browser once
    viewer.py open [--project DIR]        human: open the browser with the session secret
    viewer.py serve [--project DIR] [--port N] [--no-browser]   foreground, for tests

Security properties:
- binds 127.0.0.1 only; rejects any Host header other than 127.0.0.1:<port> / localhost:<port>
- POST /api/confirm needs the secret (X-Authorship-Secret), an Origin equal to the viewer origin
  and Content-Type: application/json; anything else is 403
- the secret lives in .authorship/run/viewer.secret (0600); the guard keeps Claude away from it
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import index  # noqa: E402
import ledger  # noqa: E402
import queries  # noqa: E402
import rules  # noqa: E402

VIEWER_DIR = os.path.join(os.path.dirname(ledger.SCRIPTS_DIR), "viewer")
DEFAULT_PORT = 47291
MAX_BODY = 64 * 1024
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml",
                ".txt": "text/plain; charset=utf-8", ".woff2": "font/woff2", ".png": "image/png"}


# ---------------------------------------------------------------------------
# Graph payload


def _stat_key(path):
    try:
        st = os.stat(path)
        return "%d.%d" % (st.st_size, st.st_mtime_ns)
    except OSError:
        return "-"


def _etag(store):
    # The head alone is not enough: an in-place edit of an older line (tampering) keeps the head, and the
    # page must still refetch so its chain badge can show "broken at #N". Size + mtime of the ledger catch it.
    head = ledger.read_head(store)
    key = "%s:%s:%s:%s" % (head[0], head[1], _stat_key(store.ledger), _stat_key(store.annotations))
    return '"%s"' % hashlib.sha256(key.encode()).hexdigest()[:32]


def build_graph(store):
    conn = index.update(store)
    q = queries.Q.__new__(queries.Q)
    q.store, q.conn = store, conn
    chain = ledger.verify(store)
    raw = {e["seq"]: e for _, _, e in ledger.read_entries(store) if e}
    ms = {}
    for r in conn.execute("SELECT * FROM milestones ORDER BY target_seq, type"):
        ms.setdefault(str(r["target_seq"]), []).append({"type": r["type"], "tier": r["tier"], "score": r["score"],
                                                        "confirmed": r["confirmed"], "evidence_seq": r["evidence_seq"],
                                                        "annotation_id": r["annotation_id"]})
    entries, nodes = [], []
    for r in conn.execute("SELECT * FROM entries ORDER BY seq"):
        e = raw.get(r["seq"], {})
        entries.append({
            "seq": r["seq"], "ts": r["ts"], "event": r["event"], "actor": r["actor"], "session": r["session"],
            "kind": r["kind"], "tool": r["tool"], "file": r["file"], "outcome": r["outcome"], "stage": r["stage"],
            "hash": r["hash"], "tags": json.loads(r["tags_json"] or "[]"),
            "auto_tags": json.loads(r["auto_tags_json"] or "[]"), "preview": rules.short(r["text"], 600),
            "text_len": len(r["text"] or ""), "blob": e.get("blob"), "command": e.get("command"),
            "input_blob": (e.get("input") or {}).get("blob") if isinstance(e.get("input"), dict) else None,
            "response_blob": (e.get("response") or {}).get("blob") if isinstance(e.get("response"), dict) else None,
            "error": e.get("error"), "reason": e.get("reason"), "author": e.get("author"),
            "decision": e.get("decision"), "label": e.get("label"), "target_seq": e.get("target_seq"),
            "edited_label": e.get("edited_label"),
        })
    for r in conn.execute("SELECT n.*, e.ts, e.event, e.tags_json, e.auto_tags_json, e.hash, e.file, e.tool, e.outcome, e.session"
                          " FROM nodes n JOIN entries e ON e.seq = n.seq ORDER BY n.seq, n.node_id"):
        nodes.append({"id": r["node_id"], "seq": r["seq"], "hash": r["hash"], "option": r["option"],
                      "ibis": r["ibis_type"], "author": r["author"], "stage": r["stage"], "status": r["status"],
                      "maturity": r["maturity"], "label": r["label"], "event": r["event"], "ts": r["ts"],
                      "tags": json.loads(r["tags_json"] or "[]"), "auto_tags": json.loads(r["auto_tags_json"] or "[]"),
                      "file": r["file"], "tool": r["tool"],
                      "outcome": r["outcome"], "session": r["session"],
                      "milestones": ms.get(r["node_id"], []) if "." not in r["node_id"] else []})
    edges = [{"src": r["src"], "dst": r["dst"], "type": r["type"], "source": r["source"]}
             for r in conn.execute("SELECT * FROM edges ORDER BY src, dst, type, source")]
    stages = [{"name": r[0], "first_seq": r[1], "last_seq": r[2]} for r in conn.execute(
        "SELECT stage, MIN(seq), MAX(seq) FROM entries WHERE stage IS NOT NULL GROUP BY stage ORDER BY MIN(seq)")]
    claims = [r["node_id"] for r in conn.execute("SELECT node_id FROM nodes WHERE ibis_type='claim' ORDER BY seq")]
    review = [explain_item(conn, i) for i in index.review_queue(conn)]
    review_all = [explain_item(conn, i) for i in index.review_queue(conn, include_auto=True)]
    invent = inventions(q, conn, claims)
    cls = index._meta(conn, "classifier") or {"state": "unknown"}
    last = conn.execute("SELECT model FROM annotations WHERE method='auto' ORDER BY ts DESC LIMIT 1").fetchone()
    cls["last_model"] = last["model"] if last else None
    cls["labeled"] = conn.execute("SELECT COUNT(*) FROM annotations WHERE method='auto'").fetchone()[0]
    conn.close()
    return {
        "etag": _etag(store),
        "project": os.path.basename(store.project),
        "chain": {k: chain[k] for k in ("ok", "entries", "head", "broken_at", "reason", "sealed_upto", "unsealed")},
        "stages": stages, "entries": entries, "nodes": nodes, "edges": edges, "claims": claims, "review": review,
        "review_all": review_all, "inventions": invent, "classifier": cls,
        "signatures": {"human": sum(1 for e in raw.values() if e.get("actor") == "human"),
                       "signed": sum(1 for e in raw.values() if e.get("actor") == "human" and isinstance(e.get("sig"), dict))},
    }


def explain_item(conn, item):
    """A review item with the question in plain words and its score."""
    import cli

    out = dict(item)
    out["question"] = cli.explain(conn, item)
    out["score"] = cli._edge_score(conn, item)
    return out


def lineage_mode(conn):
    """The same choice the disclosure makes: confirmed + automatic links when any exist, else every rule link."""
    if conn.execute("SELECT COUNT(*) FROM edges WHERE source IN ('confirmed', 'auto')").fetchone()[0]:
        has_conf = conn.execute("SELECT COUNT(*) FROM edges WHERE source='confirmed'").fetchone()[0]
        return {"curated": True}, ("confirmed and automatic" if has_conf else "automatic")
    return {}, "rules only"


def inventions(q, conn, claims):
    """Per claim: the elements it rests on, each with its origin (human, ai, mixed) and evidence."""
    mode, mode_text = lineage_mode(conn)
    rtp = {}
    for r in conn.execute("SELECT target_seq, evidence_seq FROM milestones WHERE type='reduction_to_practice'"):
        rtp.setdefault(str(r["target_seq"]), []).append(r["evidence_seq"])
    out = []
    for c in claims:
        lin = q.lineage(c, depth=20, **mode)
        edges = lin["edges"]
        elements = []
        for n in lin["items"]:
            node = conn.execute("SELECT ibis_type, author FROM nodes WHERE node_id=?", (n["node"],)).fetchone()
            if node is None or node["ibis_type"] in ("response", "action", "argument") or n["node"] == str(c):
                continue
            origin = "ai" if n["author"] == "ai" else "human"
            modified = [e["dst"] for e in edges if e["src"] == n["node"] and e["type"] in ("modifies", "refines")
                        and (conn.execute("SELECT author FROM nodes WHERE node_id=?", (e["dst"],)).fetchone() or {"author": ""})["author"] == "ai"]
            if origin == "human" and modified:
                origin = "mixed"
            changed_by = [e["src"] for e in edges if e["dst"] == n["node"] and e["type"] in ("modifies", "refines")]
            elements.append({"node": n["node"], "seq": n["seq"], "hash": n["hash"], "text": n["text"], "origin": origin,
                             "ibis": node["ibis_type"], "builds_on_ai": modified, "changed_by": changed_by,
                             "evidence": rtp.get(n["node"], [])})
        counts = {k: sum(1 for e in elements if e["origin"] == k) for k in ("human", "mixed", "ai")}
        row = conn.execute("SELECT label FROM nodes WHERE node_id=?", (str(c),)).fetchone()
        out.append({"id": str(c), "seq": rules.node_seq(c), "hash": q._hash(rules.node_seq(c)),
                    "text": rules.entry_text(ledger.find_entry(q.store, rules.node_seq(c)) or {}, q.store) or (row["label"] if row else ""),
                    "elements": elements, "counts": counts, "links": mode_text})
    return out


def build_report(store):
    q = queries.Q(store)
    c = q.chain_status()
    lines = ["# Authorship report: %s" % os.path.basename(store.project), "",
             "Draft for attorney review. Not legal advice.", "",
             "## Chain", "",
             "- Entries: %d" % c["entries"], "- Head: `%s`" % (c["head"] or "-"),
             "- Integrity: %s" % ("verified" if c["ok"] else "BROKEN at #%s (%s)" % (c["broken_at"], c["reason"])),
             "- Sealed by external anchors up to #%d (%d unsealed)" % (c["sealed_upto"], c["unsealed"]), ""]
    stages = q.conn.execute("SELECT stage, MIN(seq), MAX(seq) FROM entries WHERE stage IS NOT NULL GROUP BY stage ORDER BY 2").fetchall()
    if stages:
        lines += ["## Stages", ""] + ["- %s: #%d to #%d" % tuple(s) for s in stages] + [""]
    lines += ["## Milestones", "", "| Entry | Milestone | Tier | Score | Confirmation |", "|---|---|---|---|---|"]
    for m in q.milestones()["items"]:
        if m["type"] == "session_boundary":
            continue
        lines.append("| #%d (%s) | %s | %d | %s | %s |" % (m["seq"], m["hash"], m["type"], m["tier"],
                                                          "" if m["score"] is None else "%.2f" % m["score"], m["confirmation"]))
    lines.append("")
    for claim in [r[0] for r in q.conn.execute("SELECT node_id FROM nodes WHERE ibis_type='claim' ORDER BY seq")]:
        lin = q.lineage(claim, curated=True)
        lines += ["## Claim #%s lineage (confirmed and automatic edges)" % claim, ""]
        for n in lin["items"]:
            lines.append("- #%s (%s) [%s] %s" % (n["node"], n["hash"], n["author"], n["text"]))
        for a in lin["summary"]["ai_elements"]:
            mod = ", ".join("#%s" % m for m in a["modified_by_human_at"])
            lines.append("- AI-originated: #%s%s" % (a["node"], " (modified by the human at %s)" % mod if mod else ""))
        lines.append("")
    d = q.discarded()["items"]
    if d:
        lines += ["## Discarded approaches", ""] + ["- #%s (%s) %s" % (x["node"], x["hash"], x["text"]) for x in d] + [""]
    q.conn.close()
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "authorship-viewer"
    sys_version = ""

    def log_message(self, *a):
        pass

    # helpers
    def _send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = dict(headers or {})
        if len(body) > 16 * 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            # a 5,000-node graph is ~5 MB of JSON and ~0.3 MB gzipped; HTTP-inspecting antivirus (Avast's Web
            # Shield, even on 127.0.0.1) slowed the plain one to minutes and reset it
            import gzip

            body = gzip.compress(body, 6)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:;"
                         " connect-src 'self'; frame-ancestors 'none'")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj, headers=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), headers=headers)

    def _host_ok(self):
        port = self.server.server_address[1]
        return self.headers.get("Host") in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    @property
    def store(self):
        return self.server.store

    # GET
    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "foreign Host header"})
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/graph":
                etag = _etag(self.store)
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                g = build_graph(self.store)
                return self._json(200, g, headers={"ETag": g["etag"]})
            if path == "/api/status":
                return self._json(200, ledger.verify(self.store))
            if path == "/api/report.md":
                return self._send(200, build_report(self.store), "text/markdown; charset=utf-8")
            if path.startswith("/api/entry/"):
                # full text of one entry (graph entries carry a whitespace-collapsed 600-char preview only)
                raw = path[len("/api/entry/"):]
                if not re.match(r"^[0-9]{1,12}$", raw):
                    return self._json(400, {"error": "bad seq"})
                e = ledger.find_entry(self.store, int(raw))
                if not e:
                    return self._json(404, {"error": "no such entry"})
                return self._json(200, {"seq": e["seq"], "hash": e["hash"], "text": rules.entry_text(e, self.store)})
            if path.startswith("/api/blob/"):
                sha = path[len("/api/blob/"):]
                if not ledger._HEX64.match(sha):
                    return self._json(400, {"error": "bad blob id"})
                p = self.store.blob_path(sha)
                if not os.path.exists(p):
                    return self._json(404, {"error": "no such blob"})
                with open(p, "rb") as f:
                    return self._send(200, f.read(), "text/plain; charset=utf-8")
            return self._static(path)
        except Exception as exc:
            self.store.log_error("viewer.GET", exc)
            return self._json(500, {"error": "internal error"})

    do_HEAD = do_GET

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        rel = os.path.normpath(path.lstrip("/"))
        full = os.path.realpath(os.path.join(VIEWER_DIR, rel))
        if not full.startswith(os.path.realpath(VIEWER_DIR) + os.sep) or not os.path.isfile(full):
            return self._json(404, {"error": "not found"})
        with open(full, "rb") as f:
            return self._send(200, f.read(), STATIC_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

    # POST
    def _drain(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= MAX_BODY:
                self.rfile.read(n)
        except (ValueError, OSError):
            pass

    def do_POST(self):
        if not self._host_ok():
            self._drain()
            return self._json(403, {"error": "foreign Host header"})
        if self.path.split("?", 1)[0] != "/api/confirm":
            return self._json(404, {"error": "not found"})
        port = self.server.server_address[1]
        origin_ok = self.headers.get("Origin") in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port)
        given = self.headers.get("X-Authorship-Secret") or ""
        secret_ok = hmac.compare_digest(given.encode(), self.server.secret.encode())
        ctype_ok = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() == "application/json"
        if not (origin_ok and secret_ok and ctype_ok):
            self._drain()  # an unread body makes Windows reset the connection before the 403 arrives
            return self._json(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._json(400, {"error": "bad body size"})
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            seq = int(body["target_seq"])
            target = ledger.find_entry(self.store, seq)
            if not target:
                return self._json(400, {"error": "no entry #%d" % seq})
            label = str(body.get("label") or "")[:500]
            edited = body.get("edited_label")
            edited = str(edited)[:500] if edited else None
            e = ledger.write_confirm(self.store, seq, target["hash"], body.get("annotation_id"), body.get("decision"),
                                     label, edited)
            return self._json(200, {"seq": e["seq"], "hash": e["hash"][:12]})
        except (KeyError, ValueError, TypeError) as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:
            self.store.log_error("viewer.POST", exc)
            return self._json(500, {"error": "internal error"})


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, store, port, secret):
        self.store, self.secret = store, secret
        ThreadingHTTPServer.__init__(self, ("127.0.0.1", port), Handler)



def _write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def start(store, port=None, open_browser=True):
    """Bind, write run/ files, return the server (not yet serving)."""
    os.makedirs(store.run, exist_ok=True)
    os.chmod(store.run, 0o700)
    want = DEFAULT_PORT if port is None else port
    want = int(os.environ.get("AUTHORSHIP_VIEWER_PORT", want)) if port is None else want
    secret = secrets.token_urlsafe(32)
    server, last = None, None
    for p in ([want] if want == 0 else range(want, want + 20)):
        try:
            server = ViewerServer(store, p, secret)
            break
        except OSError as exc:
            last = exc
    if server is None:
        raise last
    real = server.server_address[1]
    _write_private(os.path.join(store.run, "viewer.secret"), secret)
    _write_private(os.path.join(store.run, "viewer.port"), str(real))
    _write_private(os.path.join(store.run, "viewer.pid"), str(os.getpid()))
    if open_browser and os.environ.get("AUTHORSHIP_VIEWER_OPEN", "1") != "0":
        threading.Thread(target=webbrowser.open, args=("http://127.0.0.1:%d/#k=%s" % (real, secret),), daemon=True).start()
    return server


def open_existing(store):
    try:
        with open(os.path.join(store.run, "viewer.secret")) as f:
            secret = f.read().strip()
        with open(os.path.join(store.run, "viewer.port")) as f:
            port = int(f.read().strip())
    except (OSError, ValueError):
        sys.stderr.write("viewer is not running; start a Claude Code session or run `viewer.py daemon`\n")
        return 1
    url = "http://127.0.0.1:%d/#k=%s" % (port, secret)
    webbrowser.open(url)
    print("opened http://127.0.0.1:%d/" % port)
    return 0


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
    cmd = args[0] if args else "open"
    if cmd == "open":
        return open_existing(store)
    port = None
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    server = start(store, port=port, open_browser=(cmd == "daemon" and "--no-browser" not in args))
    if cmd == "serve":
        print(server.server_address[1], flush=True)
    try:
        server.serve_forever()
    finally:
        for n in ("viewer.pid", "viewer.port"):
            try:
                os.remove(os.path.join(store.run, n))
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
