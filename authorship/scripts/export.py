"""`authorship export`: a self-contained evidence bundle that verifies without the plugin.

    <out>/
      README.md         what this is, what it covers, how to verify it
      FORMAT.md         the record format, enough to write another verifier
      verify.py         standalone verifier (Python 3 standard library; uses openssl, ots, ssh-keygen when present)
      ledger.jsonl      the hash-chained record
      blobs/            every content blob an entry cites
      anchors/          timestamp files, RFC 3161 responses, OpenTimestamps proofs, TSA certificates
      allowed_signers   public keys of signed human entries (when any)
      SHA256SUMS        sha256 of every file above
    and <out>.zip with the same content.
"""
import os
import shutil
import zipfile

import ledger

FORMAT = """# Authorship record format

## ledger.jsonl

One JSON object per line, UTF-8. Line *n* holds the entry with `"seq": n`.

- `v`: schema version, 1 or 2. `seq`, `ts` (UTC, ISO 8601), `event`, `actor` (`human`, `ai` or `system`), `session`.
- `prev`: the previous entry's `hash`; for entry 1, 64 zeros.
- `hash`: `sha256(canonical_json(entry without "hash"))`, lowercase hex, where
  `canonical_json` is JSON with keys sorted, no ASCII escaping and the separators `,` and `:` with no spaces
  (Python: `json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`).
- `text` and `sha256`: text stored inline, with its SHA-256. Longer text keeps a `preview` and cites a blob.
- Any object `{"blob": "<sha256>", ...}` cites `blobs/<first two hex digits>/<sha256>`, whose bytes hash to that value.
- Objects with `"omitted"` hold only the sha256 and size of content that was deliberately not stored
  (what a tool returned, inside a stored transcript).

Events: `UserPromptSubmit` (the human's prompt), `ManualNote` (a note typed by the human), `Confirm` (the human's
decision on a machine label), `Stop` / `SubagentStop` (the AI's reply), `PostToolUse` / `PostToolUseFailure`
(a tool the AI ran: its input and response blobs, the file it changed and the file's hash after), `SessionStart`,
`SessionEnd` (with the transcript blob), `Anchor` (an external timestamp), `GuardBlock` (an action the guard stopped).

## Timestamps: anchors/

`<seq>-<hash16>.txt` contains:

    authorship-ledger
    project: <name>
    seq: <seq>
    hash: <entry hash>

- `.tsr`: an RFC 3161 response over the SHA-256 of the `.txt` file (`.tsq` is the request). The issuing authority's
  certificates are in `anchors/tsa/<host>/` (`cacert.pem`, `tsa.crt`).
  Check: `openssl ts -verify -data X.txt -in X.tsr -CAfile cacert.pem -untrusted tsa.crt`.
- `.ots`: an OpenTimestamps proof of the `.txt` file. Check: `ots verify -f X.txt X.ots`.

A timestamp on entry *N* covers every entry up to *N*, since each hash includes the one before.

## Signatures

A human entry may carry `"sig": {"ns": "authorship", "principal": ..., "key": "SHA256:...", "sshsig": ...}`: an
SSH signature (`ssh-keygen -Y sign`, namespace `authorship`) over `canonical_json(entry without "hash" and "sig")`.
The signature is part of the entry, so the entry hash covers it.
Check: `ssh-keygen -Y verify -f allowed_signers -I <principal> -n authorship -s sig < message`.
The key must also be recognized outside this bundle: compare its fingerprint with the one the inventor published.
"""

README = """# Authorship evidence: %(project)s

Exported %(when)s by the authorship plugin %(version)s.

- %(entries)d entries, head #%(head_seq)d `%(head)s`
- %(human)d by the human, %(ai)d by the AI (Claude), %(system)d system
- Timestamped up to #%(sealed)d (%(anchors)d anchor files)
- %(signed)s

This folder is a hash-chained record of a human working with an AI assistant: every prompt, every reply, every
tool the AI ran. It is evidence of who proposed what, and when. It is not legal advice.

## Verify it

    python3 verify.py

Python 3 with the standard library is enough for the chain and the content. For the external proofs it also uses,
when installed: `openssl` (RFC 3161 timestamps), `ots` (OpenTimestamps, Bitcoin) and `ssh-keygen` (signatures).
Every check it cannot run is listed as "unchecked", never passed silently.

The format is in FORMAT.md, enough to write an independent verifier. `SHA256SUMS` lists every file.
"""


def export(store, out=None, make_zip=True):
    """Write the bundle. Returns {"dir", "zip", "files", "head"}."""
    import signing

    res = ledger.verify(store)
    if not res["ok"]:
        raise RuntimeError("the record does not verify (broken at #%s: %s); fix that before exporting"
                           % (res["broken_at"], res["reason"]))
    entries = [e for _, _, e in ledger.read_entries(store) if e]
    head_seq, head = ledger.read_head(store)
    stamp = ledger.now_iso()[:10]
    out = os.path.abspath(out or os.path.join(store.project, "authorship-exports",
                                              "%s-evidence-%s" % (stamp, (head or "0")[:12])))
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    shutil.copyfile(store.ledger, os.path.join(out, "ledger.jsonl"))
    for sha in sorted({s for e in entries for s in ledger.iter_blob_refs(e)}):
        dst = os.path.join(out, "blobs", sha[:2], sha)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(store.blob_path(sha), dst)
    if os.path.isdir(store.anchors):
        shutil.copytree(store.anchors, os.path.join(out, "anchors"))
    if os.path.exists(signing.allowed_signers_path(store)):
        shutil.copyfile(signing.allowed_signers_path(store), os.path.join(out, "allowed_signers"))
    shutil.copyfile(os.path.join(ledger.SCRIPTS_DIR, "evidence_verify.py"), os.path.join(out, "verify.py"))
    sig = signing.verify(store, entries)
    counts = {a: sum(1 for e in entries if e.get("actor") == a) for a in ("human", "ai", "system")}
    n_anchor_files = len([n for n in os.listdir(os.path.join(out, "anchors")) if n.endswith(".txt")]) \
        if os.path.isdir(os.path.join(out, "anchors")) else 0
    with open(os.path.join(out, "README.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(README % dict(
            project=os.path.basename(store.project), when=ledger.now_iso(), version=ledger.plugin_version() or "?",
            entries=res["entries"], head_seq=head_seq, head=head, sealed=res["sealed_upto"], anchors=n_anchor_files,
            signed=("%d of %d human entries signed, by %s" % (sig["signed"], sig["human"], ", ".join(
                "%s (%s)" % kv for kv in sorted(sig["keys"].items()))) if sig["signed"] else "No signed entries"),
            **counts))
    with open(os.path.join(out, "FORMAT.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(FORMAT)
    files = []
    for d, _, names in os.walk(out):
        for n in names:
            files.append(os.path.relpath(os.path.join(d, n), out).replace(os.sep, "/"))
    files.sort()
    with open(os.path.join(out, "SHA256SUMS"), "w", encoding="utf-8", newline="\n") as f:
        for rel in files:
            with open(os.path.join(out, *rel.split("/")), "rb") as g:
                f.write("%s  %s\n" % (ledger.sha256_bytes(g.read()), rel))
    zpath = None
    if make_zip:
        zpath = out + ".zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for rel in files + ["SHA256SUMS"]:
                z.write(os.path.join(out, *rel.split("/")), os.path.join(os.path.basename(out), rel))
    return {"dir": out, "zip": zpath, "files": len(files) + 1, "head": head, "entries": res["entries"]}
