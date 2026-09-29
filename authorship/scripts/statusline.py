#!/usr/bin/env python3
"""Claude Code status line: `authorship ✓ 214 | 12 unsealed | 3 to review`.

Reads only .authorship/head.json and .authorship/index.sqlite (kept current by
the annotator daemon). Standard library only; must finish in under 50 ms.
Configure with the snippet `init` prints (statusLine.type = "command").
"""
import json
import os
import sqlite3
import sys


def project_from_stdin():
    try:
        data = json.loads(sys.stdin.read() or "{}")
        ws = data.get("workspace") or {}
        return ws.get("project_dir") or ws.get("current_dir") or data.get("cwd")
    except Exception:
        return None


def line(project):
    root = os.path.join(project, ".authorship")
    if not os.path.isdir(root):
        return ""
    try:
        with open(os.path.join(root, "head.json"), encoding="utf-8") as f:
            head = json.load(f)
    except (OSError, ValueError):
        head = {"seq": 0, "hash": None}
    chain, review = None, None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(root, "index.sqlite"), uri=True, timeout=0.02)
        rows = dict(conn.execute("SELECT key, value FROM meta WHERE key IN ('chain', 'review_count')").fetchall())
        conn.close()
        chain = json.loads(rows["chain"]) if "chain" in rows else None
        review = json.loads(rows["review_count"]) if "review_count" in rows else None
    except Exception:
        pass
    seq = head.get("seq") or 0
    if chain and chain.get("ok") is False:
        return "authorship ✗ broken at #%s" % chain.get("broken_at")
    if not chain or chain.get("head") != head.get("hash"):
        mark = "…"  # not yet verified by the annotator
        unsealed = seq - ((chain or {}).get("sealed_upto") or 0)
    else:
        mark = "✓"
        unsealed = chain.get("unsealed", 0)
    parts = ["authorship %s %d" % (mark, seq), "%d unsealed" % unsealed]
    if review is not None:
        parts.append("%d to review" % review)
    return " | ".join(parts)


def main():
    project = os.environ.get("AUTHORSHIP_PROJECT_DIR") or project_from_stdin() or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        sys.stdout.write(line(project))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
