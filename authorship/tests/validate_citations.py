#!/usr/bin/env python3
"""Check that every citation in a draft resolves to the ledger.

    validate_citations.py FILE [--project DIR] [--contribution]

A citation is `#<seq> (<hash12>)` or `#<seq>.<n> (<hash12>)` for a sub-node
(an AI option or proposal). Each must name an existing entry whose hash starts
with <hash12>; sub-nodes must exist in the index. A bare `#<digits>` without a
hash is an error. The file must carry the header "Draft for attorney review.
Not legal advice." With --contribution, unfavorable facts must come before
human-originated elements. Exit 0 when everything resolves.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import index  # noqa: E402
import ledger  # noqa: E402

HEADER = "Draft for attorney review. Not legal advice."
CITE = re.compile(r"#(\d+)(?:\.(p?\d+))?\s\(([0-9a-f]{12})\)")
BARE = re.compile(r"(?<![\w#&/])#(\d+)(?:\.p?\d+)?(?!\d|\.\d|\s\([0-9a-f]{12}\))")


def validate(path, project, contribution=False):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    store = ledger.Store(project)
    hashes = {e["seq"]: e["hash"] for _, _, e in ledger.read_entries(store) if e}
    conn = index.update(store)
    errors = []
    if HEADER not in "\n".join(text.splitlines()[:8]):
        errors.append("missing header %r near the top" % HEADER)
    n = 0
    for m in CITE.finditer(text):
        n += 1
        seq, sub, h = int(m.group(1)), m.group(2), m.group(3)
        if seq not in hashes:
            errors.append("%s: no entry #%d" % (m.group(0), seq))
        elif not hashes[seq].startswith(h):
            errors.append("%s: hash does not match entry #%d (%s)" % (m.group(0), seq, hashes[seq][:12]))
        elif sub and not conn.execute("SELECT 1 FROM nodes WHERE node_id=?", ("%d.%s" % (seq, sub),)).fetchone():
            errors.append("%s: no sub-node #%d.%s" % (m.group(0), seq, sub))
    for m in BARE.finditer(text):
        errors.append("bare citation %s without a hash" % m.group(0))
    if contribution:
        u, h = text.find("Unfavorable facts"), text.find("Human-originated")
        if u < 0 or h < 0 or u > h:
            errors.append("unfavorable facts must come before human-originated elements")
    conn.close()
    return n, errors


def main(argv):
    args = list(argv)
    project = ledger.project_dir()
    if "--project" in args:
        i = args.index("--project")
        project = os.path.abspath(args[i + 1])
        del args[i:i + 2]
    contribution = "--contribution" in args
    args = [a for a in args if a != "--contribution"]
    if not args:
        sys.stderr.write(__doc__)
        return 2
    n, errors = validate(args[0], project, contribution)
    for e in errors:
        print("ERROR " + e)
    print("%d citation(s) checked, %d error(s)" % (n, len(errors)))
    return 0 if not errors and n else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
