#!/usr/bin/env python3
"""PreToolUse guard: keeps the audited agent away from its own audit trail.

Blocks (exit 2, reason on stderr) when a tool call would read the viewer
secret, write under .authorship/, run the human-only CLI, reach the viewer
port, or rewrite .authorship/ history through git. Every block is recorded as
a GuardBlock entry. Everything else is allowed; false positives are bugs.

Standard library only. Any internal error allows the call (exit 0) and is
logged: the guard must never break the session. The permission rules written
by `init` and the sandbox are the layers that fail closed.
"""
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger  # noqa: E402
from redact import redact  # noqa: E402

FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
READ_TOOLS = ("Read", "Grep", "Glob")
PROTECTED_SCRIPTS = ("ledger.py", "anchor.py", "viewer.py")
CLI_HUMAN_ONLY = ("note", "seal", "open", "install", "review", "confirm", "consent", "restart", "--test", "--fix")
STORE_NAMES = ("ledger.jsonl", "head.json", "state.json", "annotations.jsonl", "errors.log",
               "viewer.secret", "viewer.port", "index.sqlite")
DEFAULT_VIEWER_PORT = 47291
DESTRUCTIVE = ("rm", "rmdir", "mv", "shred", "unlink", "truncate", "chmod", "chown", "chflags",
               "rsync", "find", "srm", "trash", "ln", "cp", "tar", "zip", "dd")
GLOB_CHARS = set("*?[")
_SEPARATORS = (";", "&&", "||", "|", "&", "\n", "(", ")", "|&")
_ESCAPED_DOT = re.compile(r"(?:x2e|\\056|u002e|%2e)authorship", re.I)
_PY_IMPORT = re.compile(r"(?:importledger|fromledgerimport|write_note|write_confirm|ledger\.append\()")
_SETTINGS = re.compile(r"\.claude/settings(?:\.local)?\.json")
_PLUGIN_TOGGLE = re.compile(r"\bclaude\s+plugins?\s+(?:disable|uninstall|remove|rm)\b")
_LOOPBACK = re.compile(r"(?:localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[?::1\]?|\[::\])", re.I)


class Block(Exception):
    pass


class Ask(Exception):
    pass


def _real(path, cwd):
    path = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.realpath(path)


def _inside(path, root):
    return path == root or path.startswith(root + os.sep)


def _viewer_port(store):
    try:
        with open(os.path.join(store.run, "viewer.port")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return int(os.environ.get("AUTHORSHIP_VIEWER_PORT", DEFAULT_VIEWER_PORT))


# ---------------------------------------------------------------------------
# File and read tools


def check_file_tool(tool, tin, cwd, auth):
    for key in ("file_path", "notebook_path", "path"):
        p = tin.get(key)
        if isinstance(p, str) and p and _inside(_real(p, cwd), auth):
            raise Block("%s may not touch .authorship/ (the audit trail is written only by the hooks)" % tool)


def check_read_tool(tool, tin, cwd, auth):
    run = os.path.join(auth, "run")
    for key in ("file_path", "path"):
        p = tin.get(key)
        if isinstance(p, str) and p and _inside(_real(p, cwd), run):
            raise Block("%s may not read .authorship/run/ (it holds the viewer secret)" % tool)
    pattern = tin.get("pattern") if tool == "Glob" else None
    if isinstance(pattern, str) and re.search(r"run/|viewer\.(secret|port)", pattern):
        base = tin.get("path") or cwd
        if _inside(os.path.join(_real(base, cwd), ""), auth) or ".authorship" in pattern:
            raise Block("Glob may not list .authorship/run/")


# ---------------------------------------------------------------------------
# Bash


def _tokens(cmd):
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return re.split(r"\s+|(?=[;&|()<>])|(?<=[;&|()<>])", cmd)


def _segments(tokens):
    seg = []
    for t in tokens:
        if t in _SEPARATORS or (t and set(t) <= set(";&|()")):
            if seg:
                yield seg
            seg = []
        else:
            seg.append(t)
    if seg:
        yield seg


def _strip_prefix(seg):
    """Drop env assignments, sudo, command, exec, time, nice ... before the verb."""
    i = 0
    while i < len(seg):
        t = seg[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            i += 1
        elif t in ("sudo", "command", "exec", "time", "nice", "nohup", "env", "builtin", "xargs", "doas"):
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 1
        else:
            break
    return seg[i:]


def _is_ancestor_or_self(path, target):
    return target == path or target.startswith(path.rstrip(os.sep) + os.sep)


def check_bash(cmd, cwd, auth, store, project):
    low = cmd.lower()
    squashed = re.sub(r"[\s'\"\\`+]", "", low)

    # 1. Any mention of the store, including obvious obfuscation.
    if ".authorship" in low or ".authorship" in squashed or _ESCAPED_DOT.search(squashed):
        raise Block("Bash commands may not reference .authorship/ (the audit trail is written only by the hooks)")
    if _PY_IMPORT.search(squashed):
        raise Block("Bash may not drive the ledger module directly")

    # 2. The viewer port.
    port = str(_viewer_port(store))
    if re.search(r"(?<!\d)%s(?!\d)" % port, cmd) and (_LOOPBACK.search(cmd) or re.search(r":%s(?!\d)" % port, cmd)
                                                        or "/dev/tcp" in cmd or re.search(r"\b(nc|ncat|socat|telnet)\b", cmd)):
        raise Block("Bash may not contact the authorship viewer (port %s); confirmations are made by the human in the browser" % port)

    tokens = _tokens(cmd)
    here = cwd
    for seg in _segments(tokens):
        seg = _strip_prefix(seg)
        if not seg:
            continue
        verb = os.path.basename(seg[0])

        # 3. The human-only CLI and the viewer server.
        for i, t in enumerate(seg):
            base = os.path.basename(t)
            if base in PROTECTED_SCRIPTS:
                if base == "anchor.py" and seg[i + 1:] == ["seal"] and _real(t, here) == os.path.join(ledger.SCRIPTS_DIR, "anchor.py"):
                    continue  # the seal skill: timestamps the current head, writes no human entry
                raise Block("Bash may not run %s (human-only CLI or viewer server)" % base)

            if (i == 0 and base == "authorship") or base == "cli.py":
                sub = next((x for x in seg[i + 1:] if x in CLI_HUMAN_ONLY), None)  # flags may come first
                if sub:
                    raise Block("Bash may not run `authorship %s` (human-only command)" % sub)

        # 4. Globs that would expand to .authorship.
        for t in seg[1:]:
            if GLOB_CHARS & set(t) or "{" in t:
                for comp in t.replace("\\", "").split("/"):
                    # the shell only matches a leading dot literally
                    if comp.startswith(".") and (GLOB_CHARS & set(comp)) and fnmatch.fnmatchcase(".authorship", comp):
                        raise Block("glob %r would expand to .authorship/" % t)

        # 5. Paths: symlinks into the store, or destructive commands on an ancestor.
        paths = [t for t in seg[1:] if t and not t.startswith("-") and t not in ("<", ">", ">>")]
        for t in paths:
            if not (GLOB_CHARS & set(t)) and _inside(_real(t, here), auth):
                raise Block("path %r resolves into .authorship/" % t)
        if verb in DESTRUCTIVE:
            check_destructive(verb, seg, paths, here, auth)
        if verb == "git":
            check_git(seg, here, auth, project)

        if verb == "cd" and len(seg) > 1 and not seg[1].startswith("-"):
            here = _real(seg[1], here)

    # 6. Settings and plugin toggles: ask the human rather than block.
    if _SETTINGS.search(cmd):
        raise Ask("This command touches Claude Code settings, which hold the authorship protections.")
    if _PLUGIN_TOGGLE.search(cmd):
        raise Ask("This command would disable or remove the authorship plugin.")


def check_destructive(verb, seg, paths, here, auth):
    if verb == "find":
        acts = any(t in ("-delete", "-exec", "-execdir", "-ok") for t in seg)
        if not acts:
            return
        names = [seg[i + 1] for i, t in enumerate(seg[:-1]) if t in ("-name", "-iname", "-path", "-ipath", "-regex")]
        starts = []
        for t in seg[1:]:
            if t.startswith("-") or t in ("!", "("):
                break
            starts.append(t)
        starts = starts or ["."]
        hits_store = any(_is_ancestor_or_self(_real(s, here), auth) for s in starts)
        if not hits_store:
            return
        if not names:
            raise Block("find with -delete/-exec over a directory that contains .authorship/")
        for pat in names:
            for n in (".authorship",) + STORE_NAMES:
                if fnmatch.fnmatchcase(n, pat.split("/")[-1] or pat):
                    raise Block("find pattern %r matches authorship files" % pat)
        return
    if verb == "rsync" and not any(t.startswith("--delete") or t == "--remove-source-files" for t in seg):
        return
    if verb in ("cp", "tar", "zip", "dd", "ln"):
        return  # only dangerous when a path resolves into the store, handled above
    if verb == "mv" and len(paths) >= 2:
        paths = paths[:-1]  # moving something into an ancestor directory is harmless; moving the ancestor is not
    for t in paths:
        if GLOB_CHARS & set(t):
            continue
        if _is_ancestor_or_self(_real(t, here), auth):
            raise Block("%s on %r would affect .authorship/" % (verb, t))


def _git(project, *args):
    try:
        r = subprocess.run(["git", "-C", project] + list(args), capture_output=True, text=True, timeout=4)
        return r.returncode, r.stdout
    except Exception:
        return 1, ""


def _store_dirty(project):
    code, out = _git(project, "status", "--porcelain", "--", ".authorship")
    return code == 0 and bool(out.strip())


def _pathspec_covers(args, here, auth):
    specs = [a for a in args if not a.startswith("-")]
    for a in specs:
        if a.startswith(":") or GLOB_CHARS & set(a):
            return True
        if _is_ancestor_or_self(_real(a, here), auth) or _inside(_real(a, here), auth):
            return True
    return False


def check_git(seg, here, auth, project):
    i = 1
    repo = here
    while i < len(seg) and seg[i].startswith("-"):
        if seg[i] in ("-C",) and i + 1 < len(seg):
            repo = _real(seg[i + 1], here)
            i += 2
        elif seg[i] in ("-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(seg):
            i += 2
        else:
            i += 1
    if i >= len(seg):
        return
    sub, args = seg[i], seg[i + 1:]
    if not os.path.isdir(os.path.join(project, ".git")) and ledger._find_git_dir(project) is None:
        return
    if sub in ("filter-branch", "filter-repo"):
        raise Block("git %s rewrites history, including .authorship/" % sub)
    if sub == "reset" and any(a in ("--hard", "--merge", "--keep") for a in args):
        target = next((a for a in args if not a.startswith("-")), "HEAD")
        code, out = _git(project, "diff", "--name-only", target, "HEAD", "--", ".authorship")
        if _store_dirty(project) or (code == 0 and out.strip()) or code != 0:
            raise Block("git reset --hard would rewrite .authorship/")
        return
    if sub in ("checkout", "restore"):
        if sub == "restore" and "--staged" in args and not any(a in ("--worktree", "-W") for a in args):
            return
        if "--" in args:
            specs = args[args.index("--") + 1:]
        elif sub == "restore":
            specs = [a for a in args if not a.startswith("-")]
        else:
            specs = [a for a in args if a in (".", "*") or a.startswith("./") or a.startswith(":")]
        if specs and _pathspec_covers(specs, here, auth) and _store_dirty(project):
            raise Block("git %s would discard uncommitted .authorship/ entries" % sub)
        return
    if sub == "stash":
        action = args[0] if args and not args[0].startswith("-") else "push"
        if action not in ("push", "save"):
            return
        specs = args[args.index("--") + 1:] if "--" in args else []
        if (not specs or _pathspec_covers(specs, here, auth)) and _store_dirty(project):
            raise Block("git stash would take uncommitted .authorship/ entries out of the working tree")
        return
    if sub == "clean":
        if not any(a.startswith("-") and "f" in a.lstrip("-") or a == "--force" for a in args):
            return
        specs = [a for a in args if not a.startswith("-")]
        if not specs or _pathspec_covers(specs, here, auth):
            raise Block("git clean would delete untracked .authorship/ files")
        return
    if sub == "commit" and "--amend" in args:
        code, out = _git(project, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "HEAD", "--", ".authorship")
        if code != 0 or out.strip():
            raise Block("git commit --amend would rewrite a commit that recorded .authorship/")
        return
    if sub == "rebase":
        if any(a in ("--continue", "--abort", "--skip", "--quit", "--edit-todo", "--show-current-patch") for a in args):
            return
        if "--root" in args:
            rng = ["HEAD"]
        else:
            upstream = next((a for a in args if not a.startswith("-")), "@{upstream}")
            rng = ["%s..HEAD" % upstream]
        code, out = _git(project, "log", "--format=%H", "-n", "1", *rng, "--", ".authorship")
        if code != 0 or out.strip():
            raise Block("git rebase would rewrite commits that recorded .authorship/")
        return


# ---------------------------------------------------------------------------


def evaluate(payload):
    """Return None to allow, ("block", reason) or ("ask", reason)."""
    store = ledger.Store(ledger.project_dir(payload))
    if not store.exists():
        return None
    auth = os.path.realpath(store.root)
    cwd = payload.get("cwd") or store.project
    tool = payload.get("tool_name") or ""
    tin = payload.get("tool_input") or {}
    if not isinstance(tin, dict):
        return None
    try:
        if tool in FILE_TOOLS:
            check_file_tool(tool, tin, cwd, auth)
        elif tool in READ_TOOLS:
            check_read_tool(tool, tin, cwd, auth)
        elif tool == "Bash" and isinstance(tin.get("command"), str):
            check_bash(tin["command"], cwd, auth, store, store.project)
    except Block as b:
        return ("block", str(b))
    except Ask as a:
        return ("ask", str(a))
    return None


def _subject(payload):
    tin = payload.get("tool_input") or {}
    if not isinstance(tin, dict):
        return None
    s = tin.get("command") or tin.get("file_path") or tin.get("notebook_path") or tin.get("path") or tin.get("pattern")
    return redact(s)[:2000] if isinstance(s, str) else None


def main():
    payload = None
    try:
        payload = json.loads(sys.stdin.read())
        verdict = evaluate(payload)
    except BaseException as exc:  # noqa: B902
        store = ledger.Store(ledger.project_dir(payload if isinstance(payload, dict) else None))
        if store.exists():
            store.log_error("guard", exc)
        return 0
    if verdict is None:
        return 0
    kind, reason = verdict
    store = ledger.Store(ledger.project_dir(payload))
    if kind == "ask":
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "ask",
            "permissionDecisionReason": "authorship: " + reason}}))
        return 0
    try:
        ledger.append(store, "GuardBlock", "system", payload.get("session_id"), {
            "tool": payload.get("tool_name"), "reason": reason, "command_or_path": _subject(payload)})
    except Exception as exc:
        store.log_error("guard.record", exc)
    sys.stderr.write("authorship guard: blocked. %s.\n" % reason)
    return 2


if __name__ == "__main__":
    sys.exit(main())
