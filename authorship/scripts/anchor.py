#!/usr/bin/env python3
"""External anchoring of the ledger head: OpenTimestamps and RFC 3161.

    anchor.py run     [--project DIR]   anchor the current head now (foreground)
    anchor.py seal    [--project DIR]   the seal skill: anchor in the background, print what is anchored
    anchor.py auto    [--project DIR]   at session end: like run, but silent and only when a method is available
    anchor.py upgrade [--project DIR]   `ots upgrade` pending stamps; write `complete` when attested
    anchor.py verify  [--project DIR]   check stored anchors (same as `ledger.py verify --anchors`)

Files go to .authorship/anchors/<seq>-<hash16>.{txt,tsq,tsr,ots}. Each run writes
an Anchor entry with status "pending", and another with status "complete" once
every method has its proof. Network use: the TSA request (and `ots`), only here.

Env: AUTHORSHIP_TSA (default https://freetsa.org/tsr, "off" to disable),
AUTHORSHIP_TSA_CAFILE / AUTHORSHIP_TSA_CERT (to verify responses), AUTHORSHIP_OTS=0 to skip ots.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger  # noqa: E402

DEFAULT_TSA = "https://freetsa.org/tsr"
FREETSA_CERTS = {"cacert.pem": "https://freetsa.org/files/cacert.pem", "tsa.crt": "https://freetsa.org/files/tsa.crt"}
_NAME = re.compile(r"^(\d+)-([0-9a-f]{16})\.txt$")


def _run(argv, timeout=60, **kw):
    return subprocess.run(argv, capture_output=True, timeout=timeout, **kw)


OTS_HOME = os.environ.get("AUTHORSHIP_OTS_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share", "authorship", "ots")


def ots_bin():
    """The `ots` client: on the PATH, or where `authorship doctor --fix` installs it."""
    local_bin = os.environ.get("AUTHORSHIP_BIN_DIR") or os.path.join(os.path.expanduser("~"), ".local", "bin")
    for c in (shutil.which("ots"), os.path.join(local_bin, "ots"),
              os.path.join(OTS_HOME, "bin", "ots"), os.path.join(OTS_HOME, "Scripts", "ots.exe")):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def ots_available():
    return os.environ.get("AUTHORSHIP_OTS", "1") != "0" and ots_bin() is not None


def tsa_url():
    return os.environ.get("AUTHORSHIP_TSA", DEFAULT_TSA)


def anchor_text(store, seq, h):
    return "authorship-ledger\nproject: %s\nseq: %d\nhash: %s\n" % (os.path.basename(store.project), seq, h)


def parse_anchor_text(text):
    seq = re.search(r"^seq: (\d+)$", text, re.M)
    h = re.search(r"^hash: ([0-9a-f]{64})$", text, re.M)
    return (int(seq.group(1)), h.group(1)) if seq and h else (None, None)


def _tsa_certs(store, url):
    """(cafile, untrusted) for verifying responses, or (None, None)."""
    ca, cert = os.environ.get("AUTHORSHIP_TSA_CAFILE"), os.environ.get("AUTHORSHIP_TSA_CERT")
    if ca:
        return ca, cert
    host = re.sub(r"[^A-Za-z0-9.-]", "_", url.split("//", 1)[-1].split("/", 1)[0])
    d = os.path.join(store.anchors, "tsa", host)
    ca, cert = os.path.join(d, "cacert.pem"), os.path.join(d, "tsa.crt")
    if os.path.exists(ca):
        return ca, (cert if os.path.exists(cert) else None)
    return None, None


def _fetch_freetsa_certs(store):
    d = os.path.join(store.anchors, "tsa", "freetsa.org")
    os.makedirs(d, exist_ok=True)
    for name, url in FREETSA_CERTS.items():
        p = os.path.join(d, name)
        if not os.path.exists(p):
            with urllib.request.urlopen(url, timeout=30) as r:
                data = r.read()
            with open(p, "wb") as f:
                f.write(data)


def rfc3161(store, txt, base, url):
    tsq, tsr = base + ".tsq", base + ".tsr"
    r = _run(["openssl", "ts", "-query", "-data", txt, "-sha256", "-cert", "-out", tsq])
    if r.returncode != 0:
        raise RuntimeError("openssl ts -query failed: %s" % r.stderr.decode(errors="replace")[:300])
    with open(tsq, "rb") as f:
        body = f.read()
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/timestamp-query"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    with open(tsr, "wb") as f:
        f.write(data)
    check = check_tsr(store, txt, tsr, url)
    if not check["ok"]:
        raise RuntimeError("TSA response rejected: %s" % check["reason"])
    return check


def _status(tsr):
    r = _run(["openssl", "ts", "-reply", "-in", tsr, "-text"])
    m = re.search(r"Status: (\w+)", r.stdout.decode(errors="replace"))
    return m.group(1) if m else None


def covers(tsr_bytes, digest_hex):
    """True when the DER response carries this SHA-256 as its message imprint (OCTET STRING, 32 bytes).
    Read from the bytes: openssl's text dump drops trailing spaces and NULs from the hex, which made a
    text comparison fail on about 1 response in 70."""
    return (b"\x04\x20" + bytes.fromhex(digest_hex)) in tsr_bytes


def check_tsr(store, txt, tsr, url=None):
    with open(txt, "rb") as f:
        want = hashlib.sha256(f.read()).hexdigest()
    status = _status(tsr)
    if status != "Granted":
        return {"ok": False, "reason": "TSA status %s" % status}
    with open(tsr, "rb") as f:
        tsr_bytes = f.read()
    if not covers(tsr_bytes, want):
        return {"ok": False, "reason": "timestamp imprint does not match %s" % os.path.basename(txt)}
    ca, cert = _tsa_certs(store, url or tsa_url())
    if not ca:
        return {"ok": True, "signature": "not checked (no TSA CA certificate stored)"}
    argv = ["openssl", "ts", "-verify", "-data", txt, "-in", tsr, "-CAfile", ca]
    if cert:
        argv += ["-untrusted", cert]
    r = _run(argv)
    out = (r.stdout + r.stderr).decode(errors="replace")
    if r.returncode != 0 or "Verification: OK" not in out:
        return {"ok": False, "reason": "TSA signature does not verify: %s" % out.strip()[-200:]}
    return {"ok": True, "signature": "verified"}


def ots_stamp(txt, base):
    r = _run([ots_bin(), "stamp", txt], timeout=120)
    if r.returncode != 0:
        raise RuntimeError("ots stamp failed: %s" % r.stderr.decode(errors="replace")[:300])
    os.replace(txt + ".ots", base + ".ots")


def run(store):
    """Anchor the current head. Returns a summary dict."""
    seq, h = ledger.read_head(store)
    if seq == 0:
        return {"anchored": None, "reason": "empty ledger"}
    os.makedirs(store.anchors, exist_ok=True)
    base = os.path.join(store.anchors, "%d-%s" % (seq, h[:16]))
    txt = base + ".txt"
    if not os.path.exists(txt):
        with open(txt, "w", encoding="utf-8") as f:
            f.write(anchor_text(store, seq, h))
    methods = []
    url = tsa_url()
    if url != "off" and shutil.which("openssl"):
        methods.append("rfc3161")
    if ots_available():
        methods.append("ots")
    common = {"anchored_seq": seq, "anchored_hash": h, "methods": methods, "file": os.path.basename(txt)}
    pending = ledger.append(store, "Anchor", "system", None, dict(common, status="pending"))
    done, errors = [], {}
    if "rfc3161" in methods:
        try:
            if url == DEFAULT_TSA and not os.environ.get("AUTHORSHIP_TSA_CAFILE"):
                try:
                    _fetch_freetsa_certs(store)
                except Exception as exc:
                    errors["tsa_certs"] = str(exc)[:200]
            rfc3161(store, txt, base, url)
            done.append("rfc3161")
        except Exception as exc:
            errors["rfc3161"] = str(exc)[:300]
    if "ots" in methods:
        try:
            ots_stamp(txt, base)  # attestation arrives later via `upgrade`
        except Exception as exc:
            errors["ots"] = str(exc)[:300]
    if errors:
        store.log_error("anchor", RuntimeError(json.dumps(errors)))
    final = None
    if methods and set(done) == set(methods):
        final = ledger.append(store, "Anchor", "system", None, dict(
            common, status="complete", completed_methods=done, pending_seq=pending["seq"]))
    elif done:
        final = ledger.append(store, "Anchor", "system", None, dict(
            common, status="pending", completed_methods=done, pending_seq=pending["seq"], errors=errors or None))
    return {"anchored": {"seq": seq, "hash": h}, "methods": methods, "completed": done, "errors": errors,
            "entries": [pending["seq"]] + ([final["seq"]] if final else [])}


def upgrade(store):
    """Run `ots upgrade` on pending stamps; write a complete Anchor when every method is done."""
    if not ots_bin():
        return []
    state = {}
    for _, _, e in ledger.read_entries(store):
        if e and e.get("event") == "Anchor":
            s = state.setdefault(e["anchored_seq"], {"methods": e.get("methods") or [], "done": set(), "complete": False,
                                                     "hash": e["anchored_hash"]})
            s["done"].update(e.get("completed_methods") or [])
            s["complete"] = s["complete"] or e.get("status") == "complete"
    written = []
    for seq, s in sorted(state.items()):
        if s["complete"] or "ots" not in s["methods"] or "ots" in s["done"]:
            continue
        base = os.path.join(store.anchors, "%d-%s" % (seq, s["hash"][:16]))
        if not os.path.exists(base + ".ots"):
            continue
        _run([ots_bin(), "upgrade", base + ".ots"], timeout=120)
        r = _run([ots_bin(), "verify", "-f", base + ".txt", base + ".ots"], timeout=120)
        out = (r.stdout + r.stderr).decode(errors="replace")
        if r.returncode == 0 and "Bitcoin block" in out:
            done = sorted(s["done"] | {"ots"})
            status = "complete" if set(done) >= set(s["methods"]) else "pending"
            e = ledger.append(store, "Anchor", "system", None, {
                "anchored_seq": seq, "anchored_hash": s["hash"], "methods": s["methods"], "file": os.path.basename(base) + ".txt",
                "status": status, "completed_methods": done})
            written.append(e["seq"])
    return written


def verify_anchors(store):
    """Every stored anchor must match the recomputed chain, and every proof must check."""
    chain = ledger.verify(store)
    hashes = {e["seq"]: e["hash"] for _, _, e in ledger.read_entries(store) if e}
    details, ok, reason = [], True, None
    if not chain["ok"]:
        ok, reason = False, "chain broken at #%s" % chain["broken_at"]
    names = sorted(os.listdir(store.anchors)) if os.path.isdir(store.anchors) else []
    for name in names:
        m = _NAME.match(name)
        if not m:
            continue
        txt = os.path.join(store.anchors, name)
        base = txt[:-4]
        with open(txt, encoding="utf-8") as f:
            seq, h = parse_anchor_text(f.read())
        d = {"file": name, "seq": seq}
        if seq is None or hashes.get(seq) != h:
            d["ok"] = False
            d["reason"] = "anchored head #%s does not match the recomputed ledger" % seq
        else:
            d["ok"] = True
            if os.path.exists(base + ".tsr"):
                c = check_tsr(store, txt, base + ".tsr")
                d["rfc3161"] = c.get("signature") if c["ok"] else c["reason"]
                d["ok"] = d["ok"] and c["ok"]
            if os.path.exists(base + ".ots"):
                if ots_bin():
                    r = _run([ots_bin(), "verify", "-f", txt, base + ".ots"], timeout=120)
                    out = (r.stdout + r.stderr).decode(errors="replace")
                    d["ots"] = "verified" if r.returncode == 0 and "Bitcoin block" in out else "pending or unverifiable"
                else:
                    d["ots"] = "skipped (ots not installed)"
        if not d["ok"] and ok:
            ok, reason = False, "%s: %s" % (name, d.get("reason") or d.get("rfc3161"))
        details.append(d)
    return {"ok": ok, "checked": len(details), "reason": reason, "details": details}


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
    cmd = args[0] if args else "run"
    if cmd == "run":
        print(json.dumps(run(store)))
        return 0
    if cmd == "auto":  # session end: no Anchor entry when nothing can timestamp it (no openssl, no ots)
        if (tsa_url() != "off" and shutil.which("openssl")) or ots_available():
            run(store)
        return 0
    if cmd == "seal":
        seq, h = ledger.read_head(store)
        if seq == 0:
            print("Nothing to seal: the ledger is empty.")
            return 0
        ledger.spawn_detached([sys.executable, os.path.abspath(__file__), "run", "--project", store.project])
        methods = [m for m, on in (("RFC 3161 (%s)" % tsa_url(), tsa_url() != "off" and shutil.which("openssl")),
                                   ("OpenTimestamps", ots_available())) if on]
        print("Sealing head #%d (%s) in the background with: %s." % (seq, h[:12], ", ".join(methods) or "no method available"))
        print("Results appear as Anchor entries; check with the chain_status tool or `ledger.py verify --anchors`.")
        return 0
    if cmd == "upgrade":
        print(json.dumps(upgrade(store)))
        return 0
    if cmd == "verify":
        res = verify_anchors(store)
        print(json.dumps(res))
        return 0 if res["ok"] else 1
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
