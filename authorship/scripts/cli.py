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
    authorship classifier [--test]  who labels the entries, where the text goes, how to change it
                                    (--test sends one made-up sentence to check the setup)
    authorship restart              restart the annotator and viewer with this terminal's settings
    authorship install [--bin-dir DIR]   put `authorship` on your PATH (~/.local/bin)
    authorship doctor [--fix]       check every piece; --fix installs what is missing

The project is found like git finds a repository: the nearest directory, from
the current one upward, that holds .authorship/. Use --project DIR to override.
Commands that act in your name (note, review, seal, open) refuse to run from Claude Code.
"""
import os
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger  # noqa: E402

HUMAN_ONLY = ("note", "seal", "open", "review", "restart")
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
    "discard_with_reason": "a discarded approach, with the reason it fails",
    "problem_fixed": "the statement of the technical problem being solved",
    "decision_with_reason": "a decision between alternatives, with its reason",
    "claim_candidate": "a claim candidate: the invention stated as a whole",
    "stage_opened": "the start of a new stage of the work",
    "reduction_to_practice": "evidence that the idea works (tests pass after failing)",
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


SETUP = """How to choose the classifier
  Claude (default)  nothing to set up: it uses the `claude` command and your login.
                    Model: export AUTHORSHIP_AUTO_MODEL=claude-haiku-4-5-20251001  (default claude-sonnet-5)
  Jev               1. Get a key from one of: TypeSafe (https://docs.typesafe.ai), OpenRouter
                       (https://openrouter.ai/keys, model typesafe/jev-1.13), or the Vercel AI Gateway.
                    2. Add it to your shell profile (~/.zshrc or ~/.bashrc), so every session has it:
                         export TYPESAFE_API_KEY=...      (TypeSafe)
                         export OPENROUTER_API_KEY=...    (OpenRouter; asks for zero data retention)
                         export AI_GATEWAY_API_KEY=...    (Vercel; asks for zero data retention)
                       With more than one, TypeSafe wins, then OpenRouter; or set AUTHORSHIP_JEV_PROVIDER.
                    3. Open a new terminal, then: authorship restart
                    The text of your entries then goes to that provider: decide that before filing.
  Force one         export AUTHORSHIP_AUTO_BACKEND=claude   (or jev)
  Turn it off       export AUTHORSHIP_AUTO=0   (tags you type and the rules keep working)
After any change: authorship restart  (the annotator keeps the settings it started with)."""


def cmd_classifier(store, args, stdout=None):
    import autoclass  # noqa: F401  (imported for its side effects on sys.path users)
    import classifier
    import index

    stdout = stdout or sys.stdout
    conn = index.update(store)
    running = index._meta(conn, "classifier") or {}
    labeled = conn.execute("SELECT COUNT(*) FROM annotations WHERE method='auto'").fetchone()[0]
    conn.close()
    if os.environ.get("AUTHORSHIP_AUTO", "1") == "0":
        here, here_err = None, "off (AUTHORSHIP_AUTO=0)"
    else:
        try:
            here, here_err = classifier.default_backend(), None
        except Exception as exc:
            here, here_err = None, str(exc)
        if here is None and here_err is None:
            here_err = "no backend: no Jev key and no `claude` command on the PATH"
    info = classifier.describe_backend(here)

    def line(d):
        if not d.get("backend"):
            return None
        return "%s%s; entry text goes to %s" % (d.get("label"), " (%s)" % d["model"] if d.get("model") else "", d.get("sends_to"))

    stdout.write("Running annotator: %s\n" % (
        line(running) or {"off": "off (AUTHORSHIP_AUTO=0)", "no-backend": "not classifying: no backend",
                          "error": "failed last time: %s" % running.get("error")}.get(running.get("state"), "not seen yet")))
    if running.get("state") == "error" and running.get("backend"):
        stdout.write("  last run failed: %s\n" % running.get("error"))
    stdout.write("This terminal would use: %s\n" % (line(info) or here_err))
    if running.get("backend") and info.get("backend") and running.get("backend") != info.get("backend"):
        stdout.write("  They differ: run `authorship restart` to switch the annotator to this terminal's settings.\n")
    stdout.write("Entries labeled automatically so far: %d\n\n" % labeled)
    if "--test" in args:
        ledger.require_human("authorship classifier --test")
        if here is None:
            stdout.write("Nothing to test: %s\n" % here_err)
            return 1
        import time
        sample = {"current_stage": "", "context": [], "candidates": [],
                  "entries": [{"seq": 1, "who": "human", "event": "UserPromptSubmit",
                               "text": "Example only: detect new payments by comparing a monotonic statement counter."}]}
        t0 = time.time()
        try:
            answers, model, _ = here.classify(sample)
        except Exception as exc:
            stdout.write("Test failed: %s\n" % exc)
            return 1
        a = answers[0] if answers else {}
        stdout.write("Test OK in %.1f s with %s: the sample reads as '%s' (%s sure).\n\n" % (
            time.time() - t0, model, a.get("kind"), "%d%%" % round(100 * float(a.get("kind_confidence") or 0))))
    stdout.write(SETUP + "\n")
    return 0


def cmd_restart(store, args):
    import signal
    import time

    for name in ("annotator", "viewer"):
        pidfile = os.path.join(store.run, name + ".pid")
        try:
            with open(pidfile) as f:
                os.kill(int(f.read().strip()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
        try:
            os.remove(pidfile)
        except OSError:
            pass
    time.sleep(0.5)
    env_off = os.environ.pop("AUTHORSHIP_NO_DAEMONS", None)
    ledger.ensure_daemons(store)
    if env_off is not None:
        os.environ["AUTHORSHIP_NO_DAEMONS"] = env_off
    print("Restarted the annotator and the viewer with this terminal's settings.")
    print("The viewer opens in your browser (or run `authorship open`). Check with `authorship classifier`.")
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


def bin_dir():
    """Where the `authorship` and `ots` commands go (AUTHORSHIP_BIN_DIR overrides, for tests)."""
    return os.environ.get("AUTHORSHIP_BIN_DIR") or os.path.join(os.path.expanduser("~"), ".local", "bin")


def install_wrapper(bin_dir=None):
    """Write the `authorship` command. Returns (path, bin_dir is on PATH)."""
    bin_dir = os.path.expanduser(bin_dir or globals()["bin_dir"]())
    os.makedirs(bin_dir, exist_ok=True)
    target = os.path.join(bin_dir, "authorship")
    with open(target, "w") as f:
        f.write(WRAPPER % {"cli": os.path.abspath(__file__)})
    os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target, bin_dir in os.environ.get("PATH", "").split(os.pathsep)


def cmd_install(args):
    target, on_path = install_wrapper(_pop_flag(args, "--bin-dir"))
    print("installed %s" % target)
    if not on_path:
        d = os.path.dirname(target)
        print("%s is not on your PATH. Add this to your shell profile:\n  export PATH=\"%s:$PATH\"" % (d, d))
    return 0


# ---------------------------------------------------------------------------
# doctor: check every piece, say what is missing and how to fix it

def shell_profile():
    shell = os.path.basename(os.environ.get("SHELL", ""))
    name = ".zshrc" if shell == "zsh" else ".bash_profile" if sys.platform == "darwin" and shell == "bash" else ".bashrc"
    return os.path.join(os.path.expanduser("~"), name)


PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"  # authorship'


def add_path_line():
    """Append ~/.local/bin to the PATH in the shell profile, once. Returns the profile path, or None if present."""
    prof = shell_profile()
    try:
        with open(prof) as f:
            if ".local/bin" in f.read():
                return None
    except OSError:
        pass
    with open(prof, "a") as f:
        f.write("\n" + PATH_LINE + "\n")
    return prof


def install_ots():
    """OpenTimestamps client in a private virtualenv, linked as ~/.local/bin/ots. Returns the path."""
    import subprocess
    import anchor

    venv = anchor.OTS_HOME
    if not os.path.exists(os.path.join(venv, "bin", "python")):
        subprocess.run([sys.executable, "-m", "venv", venv], check=True, capture_output=True, timeout=300)
    subprocess.run([os.path.join(venv, "bin", "python"), "-m", "pip", "install", "--quiet", "--upgrade",
                    "opentimestamps-client"], check=True, capture_output=True, timeout=600)
    os.makedirs(bin_dir(), exist_ok=True)
    link = os.path.join(bin_dir(), "ots")
    if not os.path.exists(link):
        os.symlink(os.path.join(venv, "bin", "ots"), link)
    return link


def _sandbox_on(project):
    for p in (os.path.join(os.path.expanduser("~"), ".claude", "settings.json"),
              os.path.join(project or "", ".claude", "settings.json"), os.path.join(project or "", ".claude", "settings.local.json")):
        try:
            import json as _j
            with open(p) as f:
                if ((_j.load(f).get("sandbox") or {}).get("enabled")):
                    return True
        except (OSError, ValueError, AttributeError):
            pass
    return False


def doctor_checks(store=None):
    """[(state, item, detail, fix)] with state 'ok' | 'fix' (doctor --fix can do it) | 'todo' (you do it) | 'info'."""
    import shutil
    import anchor
    import classifier

    out = []
    v = sys.version_info
    out.append(("ok" if v >= (3, 9) else "todo", "Python", "%d.%d" % (v[0], v[1]), "install Python 3.9 or later"))
    wrapper = os.path.join(bin_dir(), "authorship")
    on_path = bin_dir() in os.environ.get("PATH", "").split(os.pathsep)
    if not os.path.exists(wrapper):
        out.append(("fix", "authorship command", "not installed", "installs ~/.local/bin/authorship"))
    elif not on_path:
        out.append(("fix", "authorship command", "installed, but ~/.local/bin is not on your PATH",
                    "adds it to %s" % shell_profile()))
    else:
        out.append(("ok", "authorship command", wrapper, ""))
    try:
        b = classifier.default_backend() if os.environ.get("AUTHORSHIP_AUTO", "1") != "0" else None
        d = classifier.describe_backend(b)
        if os.environ.get("AUTHORSHIP_AUTO", "1") == "0":
            out.append(("info", "Classifier", "off (AUTHORSHIP_AUTO=0)", "unset AUTHORSHIP_AUTO to label entries automatically"))
        elif b is None:
            out.append(("todo", "Classifier", "no `claude` command and no Jev key", "install Claude Code, or set a Jev key"))
        else:
            out.append(("ok", "Classifier", "%s (%s): text goes to %s" % (d["label"], d.get("model"), d["sends_to"]), ""))
    except Exception as exc:
        out.append(("todo", "Classifier", str(exc), "see `authorship classifier`"))
    out.append(("ok" if shutil.which("openssl") else "todo", "RFC 3161 timestamps", "openssl" if shutil.which("openssl")
                else "openssl not found", "" if shutil.which("openssl") else "install openssl"))
    ots = anchor.ots_bin()
    out.append(("ok" if ots else "fix", "Bitcoin timestamps (OpenTimestamps)", ots or "ots not installed",
                "" if ots else "installs opentimestamps-client in a private environment"))
    if store is not None and store.exists():
        v = ledger.verify(store, check_blobs=False)
        out.append(("ok" if v["ok"] else "todo", "Record", "%d entries, %s" % (v["entries"], "intact" if v["ok"] else
                    "BROKEN at #%s" % v["broken_at"]), "" if v["ok"] else "tell your attorney; do not edit the ledger"))
        try:
            import json as _j
            with open(os.path.join(store.project, ".claude", "settings.json")) as f:
                deny = (_j.load(f).get("permissions") or {}).get("deny") or []
            rules_ok = "Edit(/.authorship/**)" in deny
        except (OSError, ValueError):
            rules_ok = False
        out.append(("ok" if rules_ok else "todo", "Protection rules", "in .claude/settings.json" if rules_ok else "missing",
                    "" if rules_ok else "run /authorship:init in Claude Code"))
        alive = [n for n in ("annotator", "viewer") if ledger._pid_alive(os.path.join(store.run, n + ".pid"))]
        out.append(("ok" if len(alive) == 2 else "fix", "Background annotator and viewer",
                    "running" if len(alive) == 2 else "not running: " + ", ".join(n for n in ("annotator", "viewer") if n not in alive),
                    "" if len(alive) == 2 else "starts them"))
        out.append(("ok" if _sandbox_on(store.project) else "info", "Sandbox", "on" if _sandbox_on(store.project) else
                    "off", "" if _sandbox_on(store.project) else "recommended: run /sandbox in Claude Code"))
    else:
        out.append(("info", "Project", "not inside a recorded project", "run /authorship:init in the project, in Claude Code"))
    return out


def print_checks(checks, stdout=None):
    stdout = stdout or sys.stdout
    mark = {"ok": "\u2713", "fix": "\u2717", "todo": "\u2717", "info": "\u2022"}
    for state, item, detail, fix in checks:
        stdout.write("  %s %-38s %s\n" % (mark[state], item, detail))
        if state != "ok" and fix:
            stdout.write("      %s%s\n" % ("doctor --fix " if state == "fix" else "", fix))
    n_fix = sum(1 for c in checks if c[0] == "fix")
    return n_fix


def cmd_doctor(args, stdout=None):
    stdout = stdout or sys.stdout
    project = _pop_flag(args, "--project") or os.environ.get("AUTHORSHIP_PROJECT_DIR") or find_project()
    store = ledger.Store(project) if project else None
    fix = "--fix" in args
    if fix:
        ledger.require_human("authorship doctor --fix")
        for state, item, _, _ in doctor_checks(store):
            if state != "fix":
                continue
            try:
                if item == "authorship command":
                    path, _ = install_wrapper()
                    prof = add_path_line()
                    stdout.write("fixed: %s%s\n" % (path, "; added ~/.local/bin to %s (open a new terminal)" % prof if prof else ""))
                elif item.startswith("Bitcoin"):
                    stdout.write("installing opentimestamps-client (a minute)...\n")
                    stdout.write("fixed: %s\n" % install_ots())
                elif item.startswith("Background") and store is not None:
                    os.environ.pop("AUTHORSHIP_NO_DAEMONS", None)
                    ledger.ensure_daemons(store)
                    stdout.write("fixed: started the annotator and the viewer\n")
            except Exception as exc:
                stdout.write("could not fix %s: %s\n" % (item, exc))
        stdout.write("\n")
    stdout.write("authorship doctor%s\n" % (" (" + store.project + ")" if store is not None else ""))
    n_fix = print_checks(doctor_checks(store), stdout)
    if n_fix and not fix:
        stdout.write("\nRun `authorship doctor --fix` in your terminal to fix the %d item(s) marked doctor --fix.\n" % n_fix)
    return 0


COMMANDS = {"log": cmd_log, "status": cmd_status, "verify": cmd_verify, "note": cmd_note, "seal": cmd_seal,
            "open": cmd_open, "review": cmd_review, "classifier": cmd_classifier, "restart": cmd_restart}


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
    if cmd == "doctor":
        return cmd_doctor(rest)
    if cmd == "init":
        return cmd_init(rest)
    if cmd not in COMMANDS:
        sys.stderr.write("authorship: unknown command %r (see `authorship help`)\n" % cmd)
        return 2
    return COMMANDS[cmd](_store(rest), rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
