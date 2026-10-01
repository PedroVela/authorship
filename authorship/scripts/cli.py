#!/usr/bin/env python3
"""authorship: the human's command line for the authorship ledger.

    authorship init                 set up the current project
    authorship note TEXT...         add a note in your name (tags work: #idea, #discard ...)
    authorship log [-n N] [--all]   recent entries (#tag~ marks a tag the classifier set)
    authorship status               one line: chain, unsealed, to review
    authorship verify [--anchors]   check the chain (and the external anchors)
    authorship review [--list] [--all]   decide machine suggestions one by one (--all: also the
                                         automatic labels already counted, to correct them)
    authorship seal                 anchor the current head now
    authorship open                 open the viewer in the browser
    authorship install [--bin-dir DIR]   put `authorship` on your PATH (~/.local/bin)

The project is found like git finds a repository: the nearest directory, from
the current one upward, that holds .authorship/. Use --project DIR to override.
Commands that act in your name (note, review, seal, open) refuse to run from Claude Code.
"""
import os
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger  # noqa: E402

HUMAN_ONLY = ("note", "seal", "open", "review")
ACTORS = {"human": "you", "ai": "claude", "system": "system"}


def find_project(start=None):
    """Nearest ancestor of `start` (default: cwd) with .authorship/, or None."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(d, ".authorship")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _pop_flag(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            v = args[i + 1]
            del args[i:i + 2]
            return v
    return default


def _store(args, must_exist=True):
    project = _pop_flag(args, "--project")
    project = os.path.abspath(project) if project else (os.environ.get("AUTHORSHIP_PROJECT_DIR") or find_project())
    if not project or (must_exist and not os.path.isdir(os.path.join(project, ".authorship"))):
        sys.stderr.write("authorship: no .authorship/ here or in any parent directory. Run `authorship init` first.\n")
        sys.exit(1)
    return ledger.Store(project)


def describe(e, auto_tags=None):
    """One line per entry, for `log`. auto_tags: tags the classifier set, shown as #tag~."""
    ev, actor = e.get("event"), ACTORS.get(e.get("actor"), e.get("actor"))
    if ev in ("UserPromptSubmit", "ManualNote", "Stop", "SubagentStop"):
        kind = {"UserPromptSubmit": "prompt", "ManualNote": "note"}.get(ev, "reply")
        body = e.get("text") if e.get("text") is not None else e.get("preview") or ""
    elif e.get("kind") == "tool":
        kind = "tool"
        body = "%s %s%s" % (e.get("tool"), e.get("file") or e.get("command") or "",
                            "  [failed]" if e.get("outcome") == "failure" else "")
    elif ev == "Confirm":
        kind = "confirm"
        body = "%s #%s %s" % (e.get("decision"), e.get("target_seq"), e.get("edited_label") or e.get("label"))
    elif ev == "GuardBlock":
        kind = "blocked"
        body = "%s: %s" % (e.get("tool"), e.get("reason"))
    elif ev == "Anchor":
        kind = "anchor"
        body = "#%s %s" % (e.get("anchored_seq"), e.get("status"))
    else:
        kind = ev[0].lower() + ev[1:] if ev else "?"
        body = e.get("reason") or e.get("source") or ""
    body = " ".join(str(body).split())
    if auto_tags:
        body = " ".join(t + "~" for t in auto_tags) + "  " + body
    if len(body) > 100:
        body = body[:99] + "…"
    return "#%-5d %s  %-7s %-8s %s" % (e["seq"], e.get("ts", "")[5:16].replace("T", " "), actor, kind, body)


def cmd_log(store, args):
    n = int(_pop_flag(args, "-n", 20))
    show_all = "--all" in args
    noise = ("PostToolUse", "PostToolUseFailure", "SessionStart", "SessionEnd", "PreCompact", "Stop", "SubagentStop")
    rows = [e for _, _, e in ledger.read_entries(store) if e and (show_all or e.get("event") not in noise)]
    auto = {}
    try:
        import index
        import json as _json

        conn = index.update(store)
        auto = {r[0]: _json.loads(r[1]) for r in conn.execute("SELECT seq, auto_tags_json FROM entries WHERE auto_tags_json != '[]'")}
        conn.close()
    except Exception:
        pass  # the log must work even when the index cannot be built
    for e in rows[-n:]:
        print(describe(e, auto.get(e["seq"])))
    if auto and rows:
        print("(#tag~ = set automatically by the classifier)")
    if not rows:
        print("(no entries yet)")
    return 0


def status_line(store):
    import statusline

    return statusline.line(store.project) or "authorship: not initialized"


def cmd_status(store, args):
    import annotator

    res = annotator.run_once(store)  # classify what is new, refresh the cached chain status and review count
    print(status_line(store))
    why = {"on": "on", "off": "off (AUTHORSHIP_AUTO=0)", "no-backend": "not running: no Jev key and no `claude` command",
           "error": "failed this time; see .authorship/errors.log (it retries on the next change)"}
    import classifier

    try:
        backend = classifier.default_backend() if res["auto_state"] == "on" else None
    except Exception:
        backend = None
    print("classifier: %s%s" % (why.get(res["auto_state"], res["auto_state"]),
                                " (%s)" % backend.name if backend is not None else ""))
    return 0


def cmd_verify(store, args):
    return ledger.main(["verify"] + args + ["--project", store.project])


def cmd_note(store, args):
    author = _pop_flag(args, "--author")
    text = " ".join(args) if args else sys.stdin.read()
    if not text.strip():
        sys.stderr.write("usage: authorship note TEXT...\n")
        return 2
    e = ledger.write_note(store, text, author=author)
    tags = " ".join(e.get("tags") or [])
    print("#%d %s%s" % (e["seq"], e["hash"][:12], "  " + tags if tags else ""))
    return 0


def cmd_seal(store, args):
    import anchor

    res = anchor.run(store)
    if not res.get("anchored"):
        print("Nothing to seal: the ledger is empty.")
        return 0
    a = res["anchored"]
    print("Sealed #%d (%s) with: %s" % (a["seq"], a["hash"][:12], ", ".join(res["completed"]) or "no method completed"))
    for k, v in (res.get("errors") or {}).items():
        print("  %s: %s" % (k, v))
    return 0 if res["completed"] else 1


def cmd_open(store, args):
    import viewer

    return viewer.open_existing(store)


MILESTONE_WORDS = {
    "conception_candidate": "a conception moment: you introduced a new technical element",
    "ai_origin_element": "an AI-origin element: the idea came from Claude",
    "maturity_jump": "a maturity jump: the idea became more definite",
    "discard_with_reason": "a discarded approach: this shows the approach cannot work",
}
EDGE_WORDS = {"modifies": "modifies", "refines": "builds on", "rejects": "rejects", "derived_from": "accepts",
              "supersedes": "replaces", "discards": "discards", "supports": "supports", "objects_to": "objects to",
              "implements": "implements", "responds_to": "responds to"}
def _valid_label(label):
    import re
    import index

    if re.match(r"^milestone:[a-z_]+$", label) or re.match(r"^maturity:[a-z_]+$", label):
        return True
    return index.parse_edge_label(label) is not None


def _node_text(conn, node_id):
    r = conn.execute("SELECT label FROM nodes WHERE node_id=?", (str(node_id),)).fetchone()
    return " ".join((r["label"] if r else "").split())[:160]


def explain(conn, item):
    """Plain-words description of one review item."""
    label = item["label"]
    if label.startswith("milestone:"):
        typ = label.split(":", 1)[1]
        return "#%s is %s" % (item["target_seq"], MILESTONE_WORDS.get(typ, typ.replace("_", " ")))
    import index

    src, typ, dst = index.parse_edge_label(label)
    return "#%s %s #%s" % (src, EDGE_WORDS.get(typ, typ), dst)


def _edge_score(conn, item):
    import json

    if item["score"] is not None or not item.get("annotation_id"):
        return item["score"]
    r = conn.execute("SELECT edges_json FROM annotations WHERE id=?", (item["annotation_id"],)).fetchone()
    for ed in json.loads((r["edges_json"] if r else None) or "[]"):
        if "edge:%s:%s:%s" % (ed.get("src"), ed.get("type"), ed.get("dst")) == item["label"]:
            return ed.get("p")
    return None


def cmd_review(store, args, stdin=None, stdout=None):
    import index

    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    conn = index.update(store)
    queue = index.review_queue(conn, include_auto="--all" in args)
    # unfavorable facts first, like every other report
    queue.sort(key=lambda i: (i["label"] != "milestone:ai_origin_element", i["target_seq"], i["label"]))
    if not queue:
        stdout.write("Nothing to review.\n")
        return 0
    if "--list" in args:
        for item in queue:
            sc = _edge_score(conn, item)
            stdout.write("#%-5s %-44s %s%s\n" % (item["target_seq"], explain(conn, item), "" if sc is None else "%.2f" % sc,
                                                 "  (automatic)" if item.get("automatic") else ""))
        return 0
    if stdin is sys.stdin and not sys.stdin.isatty():
        sys.stderr.write("authorship review asks you one question per item; run it in a terminal (or use --list)\n")
        return 3
    stdout.write("%d suggestion(s). For each: [a]ccept, [r]eject, [e]dit, [s]kip, [q]uit.\n" % len(queue))
    done = 0
    for n, item in enumerate(queue, 1):
        target = ledger.find_entry(store, int(item["target_seq"]))
        if not target:
            continue
        sc = _edge_score(conn, item)
        stdout.write("\n[%d/%d] %s%s%s\n" % (n, len(queue), explain(conn, item), "" if sc is None else "  (score %.2f)" % sc,
                                            ", counted automatically" if item.get("automatic") else ""))
        stdout.write("   #%s: %s\n" % (item["target_seq"], _node_text(conn, item["target_seq"])))
        parsed = index.parse_edge_label(item["label"])
        if parsed:
            stdout.write("   #%s: %s\n" % (parsed[2], _node_text(conn, parsed[2])))
        while True:
            stdout.write("> ")
            stdout.flush()
            answer = stdin.readline()
            if not answer:
                answer = "q"
            answer = answer.strip().lower()[:1]
            if answer in ("a", "r", "e", "s", "q"):
                break
            stdout.write("   a, r, e, s or q\n")
        if answer == "q":
            break
        if answer == "s":
            continue
        edited = None
        if answer == "e":
            stdout.write("   new label (milestone:<type>, edge:<src>:<type>:<dst> or maturity:<level>): ")
            stdout.flush()
            edited = stdin.readline().strip()
            if not _valid_label(edited):
                stdout.write("   not a valid label; skipped\n")
                continue
        decision = {"a": "accept", "r": "reject", "e": "edit"}[answer]
        e = ledger.write_confirm(store, target["seq"], target["hash"], item.get("annotation_id"), decision, item["label"], edited)
        stdout.write("   recorded as #%d (%s)\n" % (e["seq"], decision))
        done += 1
    stdout.write("\n%d decision(s) recorded.\n" % done)
    return 0


def cmd_init(args):
    import init_project

    project = _pop_flag(args, "--project") or os.getcwd()
    return init_project.main(["--project", project] + args)


WRAPPER = """#!/bin/sh
# authorship command, installed by `authorship install`.
CLI="%(cli)s"
if [ ! -f "$CLI" ]; then
  # The plugin moved (for example after an update): use the newest installed copy.
  CLI=$(ls -t "$HOME"/.claude/plugins/cache/*/authorship/*/scripts/cli.py 2>/dev/null | head -n 1)
fi
if [ -z "$CLI" ] || [ ! -f "$CLI" ]; then
  echo "authorship: plugin not found; reinstall the plugin, then run its scripts/cli.py install" >&2
  exit 1
fi
exec python3 "$CLI" "$@"
"""


def cmd_install(args):
    bin_dir = os.path.expanduser(_pop_flag(args, "--bin-dir") or "~/.local/bin")
    os.makedirs(bin_dir, exist_ok=True)
    target = os.path.join(bin_dir, "authorship")
    with open(target, "w") as f:
        f.write(WRAPPER % {"cli": os.path.abspath(__file__)})
    os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print("installed %s" % target)
    if bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
        print("%s is not on your PATH. Add this to your shell profile:\n  export PATH=\"%s:$PATH\"" % (bin_dir, bin_dir))
    return 0


COMMANDS = {"log": cmd_log, "status": cmd_status, "verify": cmd_verify, "note": cmd_note, "seal": cmd_seal,
            "open": cmd_open, "review": cmd_review}


def main(argv):
    args = list(argv)
    if not args or args[0] in ("-h", "--help", "help"):
        sys.stdout.write(__doc__)
        return 0
    cmd, rest = args[0], args[1:]
    if cmd in HUMAN_ONLY:
        ledger.require_human("authorship %s" % cmd)
    if cmd == "install":
        return cmd_install(rest)
    if cmd == "init":
        return cmd_init(rest)
    if cmd not in COMMANDS:
        sys.stderr.write("authorship: unknown command %r (see `authorship help`)\n" % cmd)
        return 2
    return COMMANDS[cmd](_store(rest), rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
