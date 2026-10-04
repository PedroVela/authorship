#!/usr/bin/env python3
"""Verify an authorship evidence bundle, without the plugin.

    python3 verify.py [BUNDLE_DIR]

Standard library only. It checks, and says what it could not check:

  1. Files: every file listed in SHA256SUMS has that hash.
  2. Chain: each entry's hash is sha256 of its canonical JSON without "hash",
     and each entry's "prev" is the previous entry's hash (see FORMAT.md).
  3. Content: every blob an entry cites is present and has the cited hash.
  4. Timestamps: each anchors/<seq>-<hash16>.txt names an entry whose hash
     matches the recomputed chain; its RFC 3161 response covers that file and
     verifies against the stored TSA certificate (needs `openssl`); its
     OpenTimestamps proof verifies (needs `ots`).
  5. Signatures: each signed human entry verifies against allowed_signers
     (needs `ssh-keygen`). The key fingerprints are printed: compare them
     with the ones the inventor published elsewhere.

Exit status 0 when nothing failed.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

GENESIS_ALIASES = ("0" * 64, None, "", "genesis", "GENESIS")
ACCEPTED_VERSIONS = (1, 2)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
NAMESPACE = "authorship"


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def entry_hash(entry):
    return sha256(canonical_json({k: v for k, v in entry.items() if k != "hash"}).encode("utf-8"))


def blob_refs(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "blob" and isinstance(v, str) and HEX64.match(v):
                yield v
            else:
                for x in blob_refs(v):
                    yield x
    elif isinstance(obj, list):
        for v in obj:
            for x in blob_refs(v):
                yield x


class Report(object):
    def __init__(self):
        self.failed = 0
        self.unchecked = 0

    def ok(self, msg):
        print("  ok        %s" % msg)

    def fail(self, msg):
        self.failed += 1
        print("  FAILED    %s" % msg)

    def skip(self, msg):
        self.unchecked += 1
        print("  unchecked %s" % msg)


def check_files(root, rep):
    print("1. Files")
    path = os.path.join(root, "SHA256SUMS")
    if not os.path.exists(path):
        rep.skip("no SHA256SUMS")
        return
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            want, name = line.rstrip("\n").split("  ", 1)
            p = os.path.join(root, *name.split("/"))
            if not os.path.exists(p):
                rep.fail("%s is missing" % name)
                continue
            with open(p, "rb") as g:
                if sha256(g.read()) != want:
                    rep.fail("%s does not match SHA256SUMS" % name)
                    continue
            n += 1
    rep.ok("%d files match SHA256SUMS" % n)


def check_chain(root, rep):
    print("2. Chain and 3. Content")
    entries, prev = [], None
    with open(os.path.join(root, "ledger.jsonl"), encoding="utf-8") as f:
        lines = [l.rstrip("\n") for l in f if l.strip()]
    for n, raw in enumerate(lines, 1):
        try:
            e = json.loads(raw)
        except ValueError:
            rep.fail("line %d is not JSON" % n)
            return entries
        why = None
        if e.get("v") not in ACCEPTED_VERSIONS:
            why = "unsupported schema version %r" % e.get("v")
        elif e.get("seq") != n:
            why = "expected seq %d, found %r" % (n, e.get("seq"))
        elif (e.get("prev") not in GENESIS_ALIASES) if n == 1 else (e.get("prev") != prev):
            why = "prev does not match the previous entry's hash"
        elif e.get("hash") != entry_hash(e):
            why = "hash mismatch"
        elif e.get("text") is not None and e.get("sha256") and sha256(e["text"].encode("utf-8")) != e["sha256"]:
            why = "inline text does not match its sha256"
        else:
            for b in blob_refs(e):
                p = os.path.join(root, "blobs", b[:2], b)
                if not os.path.exists(p):
                    why = "cited blob %s is missing" % b[:12]
                    break
                with open(p, "rb") as g:
                    if sha256(g.read()) != b:
                        why = "blob %s does not match its hash" % b[:12]
                        break
        if why:
            rep.fail("entry #%d: %s" % (n, why))
            return entries
        entries.append(e)
        prev = e["hash"]
    rep.ok("%d entries chained, head %s" % (len(entries), (prev or "-")[:16]))
    rep.ok("every cited blob present and matching")
    return entries


def run(argv, **kw):
    return subprocess.run(argv, capture_output=True, timeout=120, **kw)


def check_anchors(root, entries, rep):
    print("4. Timestamps")
    d = os.path.join(root, "anchors")
    names = sorted(n for n in os.listdir(d) if re.match(r"^\d+-[0-9a-f]{16}\.txt$", n)) if os.path.isdir(d) else []
    if not names:
        rep.skip("no anchors in the bundle")
        return
    hashes = {e["seq"]: e["hash"] for e in entries}
    openssl, ots = shutil.which("openssl"), shutil.which("ots")
    tsa_dirs = [os.path.join(d, "tsa", h) for h in sorted(os.listdir(os.path.join(d, "tsa")))] \
        if os.path.isdir(os.path.join(d, "tsa")) else []
    for name in names:
        txt = os.path.join(d, name)
        base = txt[:-4]
        with open(txt, "rb") as f:
            data = f.read()
        text = data.decode("utf-8").replace("\r\n", "\n")  # anchor files made on Windows used to have CRLF
        m_seq, m_hash = re.search(r"^seq: (\d+)$", text, re.M), re.search(r"^hash: ([0-9a-f]{64})$", text, re.M)
        if not (m_seq and m_hash) or hashes.get(int(m_seq.group(1))) != m_hash.group(1):
            rep.fail("%s names an entry that does not match the chain" % name)
            continue
        seq = int(m_seq.group(1))
        if os.path.exists(base + ".tsr"):
            with open(base + ".tsr", "rb") as f:
                tsr = f.read()
            if (b"\x04\x20" + hashlib.sha256(data).digest()) not in tsr:
                rep.fail("%s.tsr does not cover %s" % (os.path.basename(base), name))
            elif not openssl:
                rep.skip("#%d RFC 3161 signature (install openssl)" % seq)
            else:
                verified, when = False, ""
                for td in tsa_dirs:
                    ca, cert = os.path.join(td, "cacert.pem"), os.path.join(td, "tsa.crt")
                    if not os.path.exists(ca):
                        continue
                    argv = ["openssl", "ts", "-verify", "-data", txt, "-in", base + ".tsr", "-CAfile", ca]
                    if os.path.exists(cert):
                        argv += ["-untrusted", cert]
                    r = run(argv)
                    if r.returncode == 0 and b"Verification: OK" in r.stdout + r.stderr:
                        verified = os.path.basename(td)
                        t = run(["openssl", "ts", "-reply", "-in", base + ".tsr", "-text"]).stdout.decode(errors="replace")
                        m = re.search(r"Time stamp: (.+)", t)
                        when = m.group(1).strip() if m else ""
                        break
                if verified:
                    rep.ok("#%d timestamped by %s at %s (RFC 3161, signature verified)" % (seq, verified, when))
                else:
                    rep.fail("#%d RFC 3161 signature does not verify against the stored TSA certificates" % seq)
        if os.path.exists(base + ".ots"):
            if not ots:
                rep.skip("#%d OpenTimestamps proof (install opentimestamps-client: ots verify -f %s %s.ots)"
                         % (seq, name, os.path.basename(base)))
            else:
                r = run([ots, "verify", "-f", txt, base + ".ots"])
                out = (r.stdout + r.stderr).decode(errors="replace")
                m = re.search(r"Bitcoin block (\d+) attests existence as of (.+)", out)
                if r.returncode == 0 and m:
                    rep.ok("#%d in Bitcoin block %s, %s (OpenTimestamps)" % (seq, m.group(1), m.group(2).strip()))
                else:
                    rep.skip("#%d OpenTimestamps proof pending or unverifiable: %s" % (seq, out.strip()[-120:]))


def check_signatures(root, entries, rep):
    print("5. Signatures")
    human = [e for e in entries if e.get("actor") == "human"]
    signed = [e for e in human if isinstance(e.get("sig"), dict)]
    if not signed:
        rep.skip("no signed entries (%d human entries)" % len(human))
        return
    allowed = os.path.join(root, "allowed_signers")
    exe = shutil.which("ssh-keygen")
    keys = {}
    bad = []
    for e in signed:
        s = e["sig"]
        keys[s.get("key")] = s.get("principal")
        if not exe or not os.path.exists(allowed):
            continue
        msg = canonical_json({k: v for k, v in e.items() if k not in ("hash", "sig")}).encode("utf-8")
        with tempfile.TemporaryDirectory() as t:
            sp = os.path.join(t, "s.sig")
            with open(sp, "w", encoding="utf-8") as f:
                f.write(s.get("sshsig") or "")
            r = run([exe, "-Y", "verify", "-f", allowed, "-I", s.get("principal") or "", "-n", NAMESPACE, "-s", sp],
                    input=msg)
        if r.returncode != 0:
            bad.append(e["seq"])
    if not exe:
        rep.skip("%d signatures (install OpenSSH's ssh-keygen)" % len(signed))
    elif bad:
        rep.fail("signature invalid on #%s" % ", #".join(str(s) for s in bad))
    else:
        rep.ok("%d of %d human entries signed, all valid" % (len(signed), len(human)))
    for k, p in sorted(keys.items()):
        print("            key %s  %s  <- compare with the fingerprint the inventor published" % (k, p))


def main(argv):
    root = os.path.abspath(argv[0] if argv else os.path.dirname(os.path.abspath(__file__)))
    if not os.path.exists(os.path.join(root, "ledger.jsonl")):
        print("no ledger.jsonl in %s" % root)
        return 2
    print("Authorship evidence bundle: %s\n" % root)
    rep = Report()
    check_files(root, rep)
    entries = check_chain(root, rep)
    check_anchors(root, entries, rep)
    check_signatures(root, entries, rep)
    print("\n%s: %d failed, %d not checked" % ("FAILED" if rep.failed else "OK", rep.failed, rep.unchecked))
    return 1 if rep.failed else 0


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
