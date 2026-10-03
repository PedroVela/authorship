#!/usr/bin/env python3
"""Set up authorship recording in a project. Idempotent; merges, never overwrites.

    init_project.py [--project DIR] [--json]

- creates .authorship/ (ledger, blobs, anchors, run)
- merges permission rules into .claude/settings.json
- appends derived and runtime paths to .gitignore
- starts the annotator and viewer now, and prints the authorship protocol, so
  recording is complete in the current session without restarting it
- prints the sandbox recommendation and a status line snippet
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger  # noqa: E402

DENY = ["Edit(/.authorship/**)", "Read(/.authorship/run/**)"]
ASK = ["Edit(/.claude/settings.json)", "Edit(/.claude/settings.local.json)"]
GITIGNORE = [".authorship/index.sqlite", ".authorship/index.sqlite-*", ".authorship/run/"]


def merge_settings(path):
    """Add the rules to settings.json, keeping every existing key. Returns the added rules."""
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if text.strip():
            data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("%s is not a JSON object" % path)
    perms = data.setdefault("permissions", {})
    added = []
    for key, rules in (("deny", DENY), ("ask", ASK)):
        current = perms.setdefault(key, [])
        for r in rules:
            if r not in current:
                current.append(r)
                added.append("%s: %s" % (key, r))
    if added or not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    return added


def merge_gitignore(path):
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    missing = [g for g in GITIGNORE if g not in lines]
    if missing:
        with open(path, "a", encoding="utf-8") as f:
            if lines and lines[-1].strip():
                f.write("\n")
            f.write("# authorship: derived index and runtime files\n")
            for g in missing:
                f.write(g + "\n")
    return missing


def statusline_snippet():
    if os.name == "nt":
        cmd = 'sh "%s" statusline.py' % os.path.join(ledger.SCRIPTS_DIR, "py.sh").replace(os.sep, "/")
    else:
        cmd = 'python3 "%s"' % os.path.join(ledger.SCRIPTS_DIR, "statusline.py")
    return json.dumps({"statusLine": {"type": "command", "command": cmd, "padding": 0}}, indent=2)


def init(project):
    store = ledger.Store(project)
    created = not store.exists()
    store.ensure()
    if not os.path.exists(store.annotations):
        open(store.annotations, "a").close()
    os.chmod(store.run, 0o700)
    added = merge_settings(os.path.join(project, ".claude", "settings.json"))
    ignored = merge_gitignore(os.path.join(project, ".gitignore"))
    return {"project": project, "created": created, "rules_added": added, "gitignore_added": ignored,
            "chain": ledger.verify(store, check_blobs=False)}


def activate(project):
    """What SessionStart would have done: start the daemons, return the protocol text.
    The session that runs init began before .authorship/ existed, so its
    SessionStart hook did nothing; this makes restarting unnecessary."""
    ledger.ensure_daemons(ledger.Store(project))
    with open(os.path.join(ledger.SCRIPTS_DIR, "protocol.md"), encoding="utf-8") as f:
        return f.read()


def setup_command():
    """Install the `authorship` command (~/.local/bin). Returns (path, on PATH)."""
    import cli

    try:
        return cli.install_wrapper()
    except OSError:
        return None, False


def python3_works():
    import shutil
    import subprocess

    exe = shutil.which("python3")
    if not exe or "WindowsApps" in exe:  # the Microsoft Store stub
        return False
    try:
        return subprocess.run([exe, "-c", "import sys; sys.exit(sys.version_info[0] != 3)"], capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def remember_python():
    """On Windows without a working `python3`, save this interpreter as AUTHORSHIP_PYTHON (user environment),
    which the lookup (MCP) server's command uses. Returns the path saved, or None."""
    import subprocess

    if os.name != "nt" or os.environ.get("AUTHORSHIP_PYTHON") or python3_works():
        return None
    try:
        subprocess.run(["setx", "AUTHORSHIP_PYTHON", sys.executable], capture_output=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return sys.executable


def main(argv):
    as_json = "--json" in argv
    project = ledger.project_dir()
    if "--project" in argv:
        project = os.path.abspath(argv[argv.index("--project") + 1])
    res = init(project)
    res["protocol"] = activate(project)
    res["command"], res["command_on_path"] = setup_command()
    res["python_saved"] = remember_python()
    if as_json:
        print(json.dumps(res))
        return 0
    print("authorship: %s .authorship/ in %s" % ("created" if res["created"] else "found", project))
    for r in res["rules_added"]:
        print("  permission added  %s" % r)
    for g in res["gitignore_added"]:
        print("  .gitignore added  %s" % g)
    if not res["rules_added"] and not res["gitignore_added"]:
        print("  settings and .gitignore already up to date")
    c = res["chain"]
    print("  chain: %d entries, %s" % (c["entries"], "ok" if c["ok"] else "BROKEN at #%s" % c["broken_at"]))
    print()
    print("Recommended: enable the sandbox (/sandbox), so the Edit deny rule on .authorship/")
    print("also becomes an OS-level write denial for Bash commands.")
    print()
    print("Optional status line. Add this to ~/.claude/settings.json or .claude/settings.local.json:")
    print(statusline_snippet())
    print()
    print("Keep this repository private: public disclosure before filing can destroy novelty.")
    print()
    if res["python_saved"]:
        print("Windows has no `python3` here: saved AUTHORSHIP_PYTHON=%s in your user environment." % res["python_saved"])
        print("Restart Claude Code once so the lookup (MCP) server starts with it. Recording works already.")
        print()
    import cli

    if res["command"] and res["command_on_path"]:
        print("Installed your command: `authorship` (%s)." % res["command"])
    elif res["command"]:
        print("Installed your command at %s, but ~/.local/bin is not on your PATH yet." % res["command"])
    print("Setup check:")
    n_fix = cli.print_checks(cli.doctor_checks(ledger.Store(project)))
    if n_fix:
        print("To fix the items marked doctor --fix, run this once in your own terminal (not in Claude Code):")
        print("  %s doctor --fix" % (res["command"] or "python3 %s" % os.path.join(ledger.SCRIPTS_DIR, "cli.py")))
    print()
    print("Recording is on for this session; the viewer is starting (no restart needed).")
    print("Authorship protocol, in effect from now on in this session:")
    print()
    print(res["protocol"].rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
