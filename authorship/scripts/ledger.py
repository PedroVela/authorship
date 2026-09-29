#!/usr/bin/env python3
"""Authorship ledger: hook entrypoint and CLI.

Standard library only (Python 3.9+). Every hook path exits 0, whatever
happens, and logs failures to .authorship/errors.log.

    ledger.py hook              read one hook payload from stdin and record it
    ledger.py session-start     SessionStart: record, reconcile, print protocol, start daemons
    ledger.py verify [--anchors] [--json]
    ledger.py note TEXT [--author NAME]         human only
    ledger.py confirm --target-seq N ...        human only
    ledger.py anchor                            human only (delegates to anchor.py)
    ledger.py status
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from redact import redact, redact_obj  # noqa: E402

SCHEMA_VERSION = 2
ACCEPTED_VERSIONS = (1, 2)
INLINE_MAX = 8 * 1024
PREVIEW_CHARS = 400
GENESIS = "0" * 64
# The prototype in reference/ was not available when this was written, so the
# first entry's `prev` is accepted in any of the usual genesis spellings.
GENESIS_ALIASES = (GENESIS, None, "", "genesis", "GENESIS")
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(SCRIPTS_DIR)
DEFAULT_SKIP_TOOLS = "Read,Glob,Grep,LS,TodoWrite"
FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
RECONCILE_IDLE_S = 3600
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Hashing


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_text(text):
    return sha256_bytes(text.encode("utf-8"))


def entry_hash(entry):
    """sha256(canonical_json(entry_without_hash)). Must never change."""
    body = {k: v for k, v in entry.items() if k != "hash"}
    return sha256_text(canonical_json(body))


def now_iso():
    t = datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


# ---------------------------------------------------------------------------
# Store


def project_dir(payload=None):
    d = os.environ.get("AUTHORSHIP_PROJECT_DIR") or os.environ.get("CLAUDE_PROJECT_DIR")
    if not d and payload:
        d = payload.get("cwd")
    return os.path.abspath(d or os.getcwd())


class Store(object):
    def __init__(self, project):
        self.project = os.path.abspath(project)
        self.root = os.path.join(self.project, ".authorship")
        self.ledger = os.path.join(self.root, "ledger.jsonl")
        self.head = os.path.join(self.root, "head.json")
        self.state = os.path.join(self.root, "state.json")
        self.blobs = os.path.join(self.root, "blobs")
        self.anchors = os.path.join(self.root, "anchors")
        self.annotations = os.path.join(self.root, "annotations.jsonl")
        self.index = os.path.join(self.root, "index.sqlite")
        self.run = os.path.join(self.root, "run")
        self.errors = os.path.join(self.root, "errors.log")

    def exists(self):
        return os.path.isdir(self.root)

    def ensure(self):
        for d in (self.root, self.blobs, self.anchors, self.run):
            os.makedirs(d, exist_ok=True)
        if not os.path.exists(self.ledger):
            open(self.ledger, "a").close()

    def blob_path(self, sha):
        return os.path.join(self.blobs, sha[:2], sha)

    def log_error(self, where, exc):
        try:
            os.makedirs(self.root, exist_ok=True)
            with open(self.errors, "a", encoding="utf-8") as f:
                f.write("%s %s %s: %s\n" % (now_iso(), where, type(exc).__name__, exc))
        except Exception:
            pass


class _Lock(object):
    """Exclusive advisory lock around read-head / append / write-head."""

    def __init__(self, store, name="ledger"):
        self.path = os.path.join(store.run, name + ".lock")
        self.fd = None

    def __enter__(self):
        import fcntl

        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        import fcntl

        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def _read_last_line(path):
    """Return (last complete line, torn tail bytes or b'')."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, b""
    if size == 0:
        return None, b""
    with open(path, "rb") as f:
        chunk = 4096
        pos = size
        buf = b""
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            # Need the newline that ends the last complete line plus the one before it.
            stripped = buf[:-1] if buf.endswith(b"\n") else buf
            if b"\n" in stripped or pos == 0:
                break
            chunk *= 2
    torn = b""
    if not buf.endswith(b"\n"):
        cut = buf.rfind(b"\n")
        torn = buf[cut + 1:] if cut >= 0 else buf
        buf = buf[: cut + 1] if cut >= 0 else b""
    lines = buf.rstrip(b"\n").split(b"\n")
    last = lines[-1] if lines and lines[-1] else None
    return (last.decode("utf-8") if last else None), torn


def read_head(store):
    line, _ = _read_last_line(store.ledger)
    if not line:
        return 0, GENESIS
    e = json.loads(line)
    return int(e["seq"]), e["hash"]


def _repair_torn_tail(store):
    """A crash mid-write leaves a partial line with no valid hash. Move it to
    errors.log and truncate, so the chain can continue. Complete lines are
    never touched."""
    _, torn = _read_last_line(store.ledger)
    if not torn:
        return
    size = os.path.getsize(store.ledger)
    with open(store.errors, "a", encoding="utf-8") as f:
        f.write("%s torn-tail removed (%d bytes): %r\n" % (now_iso(), len(torn), torn[:2000]))
    with open(store.ledger, "r+b") as f:
        f.truncate(size - len(torn))


def append(store, event, actor, session=None, fields=None):
    """Append one hash-chained entry. Returns the entry dict."""
    store.ensure()
    with _Lock(store):
        _repair_torn_tail(store)
        seq, prev = read_head(store)
        entry = {
            "v": SCHEMA_VERSION,
            "seq": seq + 1,
            "ts": now_iso(),
            "event": event,
            "actor": actor,
            "session": session,
            "prev": prev,
        }
        for k, v in (fields or {}).items():
            if k not in entry and k != "hash":
                entry[k] = v
        entry["hash"] = entry_hash(entry)
        with open(store.ledger, "a", encoding="utf-8") as f:
            f.write(canonical_json(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _write_json_atomic(store.head, {"seq": entry["seq"], "hash": entry["hash"]})
    return entry


def _write_json_atomic(path, obj):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(canonical_json(obj))
    os.replace(tmp, path)


def put_blob(store, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    sha = sha256_bytes(data)
    path = store.blob_path(sha)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    return sha


def blob_ref(store, obj):
    """Store a redacted JSON-able object as a blob; return {"blob", "bytes"}."""
    if obj is None:
        return None
    data = canonical_json(redact_obj(obj)).encode("utf-8")
    return {"blob": put_blob(store, data), "bytes": len(data)}


def text_fields(store, text):
    """Inline text when <= 8 KB, else preview + blob. Always redacted first."""
    text = redact(text or "")
    data = text.encode("utf-8")
    sha = sha256_bytes(data)
    if len(data) <= INLINE_MAX:
        return {"text": text, "sha256": sha}
    put_blob(store, data)
    return {"preview": text[:PREVIEW_CHARS], "sha256": sha, "blob": sha, "bytes": len(data)}


def read_entries(store):
    """Yield (line_no, raw_line, entry_or_None)."""
    if not os.path.exists(store.ledger):
        return
    with open(store.ledger, "r", encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            raw = line.rstrip("\n")
            try:
                yield n, raw, json.loads(raw)
            except ValueError:
                yield n, raw, None


def load_state(store):
    try:
        with open(store.state, "r", encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("sessions", {})
    s.setdefault("transcripts", {})
    return s


def save_state(store, s):
    _write_json_atomic(store.state, s)


# ---------------------------------------------------------------------------
# Tags

CANONICAL_TAGS = ("idea", "claim", "decision", "problem", "hypothesis", "discard", "stage")
TAG_ALIASES = {
    "problema": "problem",
    "hipotesis": "hypothesis",
    "hipótesis": "hypothesis",
    "descarte": "discard",
    "etapa": "stage",
}
_TAG_RE = re.compile(r"(?<![\w#/])#([A-Za-zÀ-ÿ]+)")
_STAGE_NAME_RE = re.compile(r'[ \t]+(?:"([^"\n]+)"|([^\s#]+))')


def parse_tags(text):
    """Return (canonical tags in order of first use, stage name or None).
    The original text is never altered."""
    tags, stage = [], None
    for m in _TAG_RE.finditer(text or ""):
        name = m.group(1).lower()
        name = TAG_ALIASES.get(name, name)
        if name not in CANONICAL_TAGS:
            continue
        tag = "#" + name
        if tag not in tags:
            tags.append(tag)
        if name == "stage" and stage is None:
            mm = _STAGE_NAME_RE.match(text, m.end())
            if mm:
                stage = mm.group(1) or mm.group(2)
    return tags, stage


# ---------------------------------------------------------------------------
# Git and plugin facts (no subprocess on the hook path)


def _find_git_dir(start):
    d = os.path.abspath(start)
    while True:
        g = os.path.join(d, ".git")
        if os.path.isdir(g):
            return g
        if os.path.isfile(g):
            with open(g, "r") as f:
                line = f.read().strip()
            if line.startswith("gitdir:"):
                p = line[7:].strip()
                return p if os.path.isabs(p) else os.path.normpath(os.path.join(d, p))
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def git_head(project):
    try:
        g = _find_git_dir(project)
        if not g:
            return None
        with open(os.path.join(g, "HEAD"), "r") as f:
            head = f.read().strip()
        if not head.startswith("ref: "):
            return head
        ref = head[5:]
        common = g
        cd = os.path.join(g, "commondir")
        if os.path.isfile(cd):
            with open(cd) as f:
                common = os.path.normpath(os.path.join(g, f.read().strip()))
        for base in (g, common):
            p = os.path.join(base, ref)
            if os.path.isfile(p):
                with open(p) as f:
                    return f.read().strip()
        packed = os.path.join(common, "packed-refs")
        if os.path.isfile(packed):
            with open(packed) as f:
                for line in f:
                    parts = line.strip().split(" ")
                    if len(parts) == 2 and parts[1] == ref:
                        return parts[0]
        return None
    except Exception:
        return None


def plugin_version():
    try:
        with open(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            return json.load(f).get("version")
    except Exception:
        return None


def hooks_sha():
    try:
        with open(os.path.join(PLUGIN_ROOT, "hooks", "hooks.json"), "rb") as f:
            return sha256_bytes(f.read())
    except Exception:
        return None


def file_sha(path, limit=64 * 1024 * 1024):
    try:
        if os.path.isfile(path) and os.path.getsize(path) <= limit:
            with open(path, "rb") as f:
                return sha256_bytes(f.read())
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Transcript reading (Stop / SubagentStop)


def _assistant_texts(obj):
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return []
    msg = obj.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return [content] if content.strip() else []
    out = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text" and (block.get("text") or "").strip():
            out.append(block["text"])
    return out


def read_new_assistant_text(path, offset, main_thread=True):
    """Read complete lines from byte offset. Returns (texts, new_offset)."""
    texts = []
    try:
        size = os.path.getsize(path)
    except OSError:
        return texts, offset
    if offset > size:  # transcript rewritten or truncated; start over
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        return texts, offset
    for raw in data[: end + 1].split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if main_thread and isinstance(obj, dict) and obj.get("isSidechain"):
            continue
        texts.extend(_assistant_texts(obj))
    return texts, offset + end + 1


# ---------------------------------------------------------------------------
# Hook handlers


def _session(p):
    return p.get("session_id")


def _skip_tools():
    raw = os.environ.get("AUTHORSHIP_SKIP_TOOLS", DEFAULT_SKIP_TOOLS)
    return set(t.strip() for t in raw.split(",") if t.strip())


def _tool_outcome(event, resp):
    if event == "PostToolUseFailure":
        return "failure"
    if isinstance(resp, dict):
        if resp.get("interrupted"):
            return "interrupted"
        if resp.get("is_error") or resp.get("isError") or resp.get("success") is False:
            return "failure"
        for key in ("exit_code", "exitCode", "returnCode", "return_code", "code"):
            v = resp.get(key)
            if isinstance(v, int) and not isinstance(v, bool):
                return "failure" if v != 0 else "success"
    return "success"


def h_session_start(store, p):
    sid = _session(p)
    with _Lock(store, "state"):
        s = load_state(store)
        rec = s["sessions"].setdefault(sid or "?", {})
        rec.update({"transcript": p.get("transcript_path"), "started": now_iso(), "ended": False})
        save_state(store, s)
    model = p.get("model")
    if isinstance(model, dict):
        model = model.get("id") or model.get("display_name")
    append(store, "SessionStart", "system", sid, {
        "source": p.get("source"),
        "model": model,
        "git_head": git_head(store.project),
        "cwd": p.get("cwd"),
        "plugin_version": plugin_version(),
        "hooks_sha": hooks_sha(),
    })


def h_prompt(store, p):
    text = p.get("prompt")
    if text is None:
        text = p.get("user_message") or ""
    tags, stage = parse_tags(text)
    fields = {"kind": "prompt", "tags": tags}
    if stage:
        fields["stage"] = stage
    fields.update(text_fields(store, text))
    append(store, "UserPromptSubmit", "human", _session(p), fields)


def h_tool(store, p):
    event = p.get("hook_event_name")
    tool = p.get("tool_name") or ""
    if tool in _skip_tools():
        return
    tin = p.get("tool_input") or {}
    resp = p.get("tool_response")
    if resp is None:
        resp = p.get("tool_output")
    fields = {
        "kind": "tool",
        "tool": tool,
        "outcome": _tool_outcome(event, resp),
        "input": blob_ref(store, tin),
        "response": blob_ref(store, resp),
        "tool_use_id": p.get("tool_use_id"),
    }
    if p.get("error") is not None:
        err = p.get("error")
        fields["error"] = redact(err if isinstance(err, str) else canonical_json(err))[:4000]
    if isinstance(tin, dict):
        path = tin.get("file_path") or tin.get("notebook_path")
        if path:
            abspath = path if os.path.isabs(path) else os.path.join(p.get("cwd") or store.project, path)
            try:
                rel = os.path.relpath(abspath, store.project)
            except ValueError:
                rel = abspath
            fields["file"] = rel if not rel.startswith("..") else abspath
            if tool in FILE_TOOLS:
                fields["file_sha_after"] = file_sha(abspath)
        if tool == "Bash" and isinstance(tin.get("command"), str):
            fields["command"] = redact(tin["command"])
    append(store, event, "ai", _session(p), fields)


def h_stop(store, p):
    event = p.get("hook_event_name")
    sid = _session(p)
    subagent = event == "SubagentStop"
    if subagent:
        tp = p.get("agent_transcript_path")
        key = tp or ("agent:%s" % (p.get("agent_id") or sid))
    else:
        tp = p.get("transcript_path")
        key = tp or ("session:%s" % sid)
    with _Lock(store, "state"):
        _stop_locked(store, p, event, sid, subagent, tp, key)


def _stop_locked(store, p, event, sid, subagent, tp, key):
    s = load_state(store)
    tr = s["transcripts"].setdefault(key, {"offset": 0, "seen": []})
    texts, new_offset = [], tr.get("offset", 0)
    if tp and os.path.exists(tp):
        texts, new_offset = read_new_assistant_text(tp, tr.get("offset", 0), main_thread=not subagent)
    seen = list(tr.get("seen", []))
    blocks = []
    for t in texts:
        h = sha256_text(t)
        if h in seen:
            continue
        blocks.append(t)
        seen.append(h)
    last = p.get("last_assistant_message")
    if not blocks and isinstance(last, str) and last.strip():
        h = sha256_text(last)
        if h not in seen:
            blocks.append(last)
            seen.append(h)
    fields = {"kind": "response", "n_blocks": len(blocks), "transcript_offset": new_offset}
    if subagent:
        fields["agent_id"] = p.get("agent_id")
        fields["agent_type"] = p.get("agent_type")
    fields.update(text_fields(store, "\n\n".join(blocks)))
    append(store, event, "ai", sid, fields)
    tr["offset"] = new_offset
    tr["seen"] = seen[-200:]
    save_state(store, s)


def h_precompact(store, p):
    append(store, "PreCompact", "system", _session(p), {"trigger": p.get("trigger")})


def _transcript_blob(store, path):
    if not path or not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = redact(f.read()).encode("utf-8")
    return {"blob": put_blob(store, data), "bytes": len(data)}


def h_session_end(store, p):
    sid = _session(p)
    append(store, "SessionEnd", "system", sid, {
        "reason": p.get("reason"),
        "git_head": git_head(store.project),
        "transcript": _transcript_blob(store, p.get("transcript_path")),
    })
    with _Lock(store, "state"):
        s = load_state(store)
        s["sessions"].setdefault(sid or "?", {})["ended"] = True
        save_state(store, s)
    if os.environ.get("AUTHORSHIP_ANCHOR") == "1":
        spawn_detached([sys.executable, os.path.join(SCRIPTS_DIR, "anchor.py"), "--project", store.project])


HANDLERS = {
    "SessionStart": h_session_start,
    "UserPromptSubmit": h_prompt,
    "PostToolUse": h_tool,
    "PostToolUseFailure": h_tool,
    "Stop": h_stop,
    "SubagentStop": h_stop,
    "PreCompact": h_precompact,
    "SessionEnd": h_session_end,
}


def record_hook(payload):
    """Record one hook payload. Returns the Store, or None if not initialized."""
    store = Store(project_dir(payload))
    if not store.exists():
        return None
    handler = HANDLERS.get(payload.get("hook_event_name"))
    if handler:
        handler(store, payload)
    return store


# ---------------------------------------------------------------------------
# SessionStart extras: reconcile, protocol, daemons


def reconcile(store, current_session):
    with _Lock(store, "state"):
        s = load_state(store)
        changed = False
        for sid, rec in s["sessions"].items():
            if sid == current_session or rec.get("ended"):
                continue
            tp = rec.get("transcript")
            if tp and os.path.exists(tp) and time.time() - os.path.getmtime(tp) < RECONCILE_IDLE_S:
                continue  # probably a live parallel session
            append(store, "SessionReconciled", "system", current_session, {
                "for_session": sid,
                "transcript": _transcript_blob(store, tp),
            })
            rec["ended"] = "reconciled"
            changed = True
        if changed:
            save_state(store, s)


def spawn_detached(argv, env=None):
    import subprocess

    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    except Exception:
        pass


def _pid_alive(pidfile):
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def ensure_daemons(store):
    if os.environ.get("AUTHORSHIP_NO_DAEMONS") == "1":
        return
    for name in ("annotator", "viewer"):
        if not _pid_alive(os.path.join(store.run, name + ".pid")):
            spawn_detached([sys.executable, os.path.join(SCRIPTS_DIR, name + ".py"), "daemon", "--project", store.project])
    if os.path.isdir(store.anchors) and any(n.endswith(".ots") for n in os.listdir(store.anchors)):
        spawn_detached([sys.executable, os.path.join(SCRIPTS_DIR, "anchor.py"), "upgrade", "--project", store.project])


def cmd_session_start(raw):
    payload = json.loads(raw) if raw.strip() else {}
    payload.setdefault("hook_event_name", "SessionStart")
    store = record_hook(payload)
    if store is None:
        return
    reconcile(store, payload.get("session_id"))
    with open(os.path.join(SCRIPTS_DIR, "protocol.md"), "r", encoding="utf-8") as f:
        sys.stdout.write(f.read())
    sys.stdout.flush()
    ensure_daemons(store)


# ---------------------------------------------------------------------------
# Verification


def iter_blob_refs(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "blob" and isinstance(v, str) and _HEX64.match(v):
                yield v
            else:
                for x in iter_blob_refs(v):
                    yield x
    elif isinstance(obj, list):
        for v in obj:
            for x in iter_blob_refs(v):
                yield x


def verify(store, check_blobs=True):
    """Walk the chain. Returns a dict: ok, entries, head, broken_at, reason."""
    prev, expected_seq, count, head = GENESIS, 1, 0, None
    result = {"ok": True, "entries": 0, "head": None, "broken_at": None, "reason": None,
              "versions": {}, "sealed_upto": 0}
    for n, raw, e in read_entries(store):
        if not raw.strip():
            continue
        fail = None
        if e is None or not isinstance(e, dict):
            fail = "line %d is not valid JSON" % n
        elif e.get("v") not in ACCEPTED_VERSIONS:
            fail = "unsupported schema version %r" % e.get("v")
        elif e.get("seq") != expected_seq:
            fail = "expected seq %d, found %r" % (expected_seq, e.get("seq"))
        elif (e.get("prev") not in GENESIS_ALIASES) if expected_seq == 1 else (e.get("prev") != prev):
            fail = "prev does not match the previous entry's hash"
        elif e.get("hash") != entry_hash(e):
            fail = "hash mismatch"
        elif check_blobs:
            for sha in iter_blob_refs(e):
                path = store.blob_path(sha)
                if not os.path.exists(path):
                    fail = "missing blob %s" % sha[:12]
                    break
                with open(path, "rb") as f:
                    if sha256_bytes(f.read()) != sha:
                        fail = "blob %s content does not match its hash" % sha[:12]
                        break
            if not fail and e.get("text") is not None and e.get("sha256") and sha256_text(e["text"]) != e["sha256"]:
                fail = "inline text does not match sha256"
        if fail:
            result.update(ok=False, broken_at=expected_seq, reason=fail)
            break
        count += 1
        prev = head = e["hash"]
        expected_seq += 1
        v = str(e.get("v"))
        result["versions"][v] = result["versions"].get(v, 0) + 1
        if e.get("event") == "Anchor" and isinstance(e.get("anchored_seq"), int) and (
                e.get("status") == "complete" or "rfc3161" in (e.get("completed_methods") or [])):
            result["sealed_upto"] = max(result["sealed_upto"], e["anchored_seq"])
    result["entries"] = count
    result["head"] = head
    result["unsealed"] = max(0, count - result["sealed_upto"])
    return result


# ---------------------------------------------------------------------------
# Human-only commands


def spawned_by_claude(env=None, check_parents=True):
    """True when this process looks like it was started by Claude Code.
    Two signals: the env marker, then the parent-process chain."""
    env = os.environ if env is None else env
    if env.get("CLAUDECODE") == "1" or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return True
    if not check_parents:
        return False
    try:
        import subprocess

        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=3).stdout
        table = {}
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3:
                table[int(parts[0])] = (int(parts[1]), parts[2])
        pid = os.getppid()
        for _ in range(32):
            if pid not in table or pid <= 1:
                break
            ppid, comm = table[pid]
            name = os.path.basename(comm).lower()
            if name == "claude" or name.startswith("claude-code") or name.startswith("claude "):
                return True
            pid = ppid
    except Exception:
        pass
    return False


def require_human(what):
    if spawned_by_claude():
        sys.stderr.write(
            "authorship: `%s` is human-only and refuses to run from Claude Code.\n"
            "Run it in your own terminal, outside any Claude Code session.\n" % what
        )
        sys.exit(3)


def default_author():
    for k in ("AUTHORSHIP_AUTHOR", "GIT_AUTHOR_NAME", "USER"):
        if os.environ.get(k):
            return os.environ[k]
    return None


def write_note(store, text, author=None, session=None, extra_tags=None, extra=None):
    tags, stage = parse_tags(text)
    for t in extra_tags or []:
        if t not in tags:
            tags.append(t)
    fields = {"kind": "note", "author": author or default_author(), "tags": tags}
    if stage:
        fields["stage"] = stage
    fields.update(text_fields(store, text))
    fields.update(extra or {})
    return append(store, "ManualNote", "human", session, fields)


def write_confirm(store, target_seq, target_hash, annotation_id, decision, label, edited_label=None):
    if decision not in ("accept", "reject", "edit"):
        raise ValueError("decision must be accept, reject or edit")
    if decision == "edit" and not edited_label:
        raise ValueError("edit requires edited_label")
    return append(store, "Confirm", "human", None, {
        "target_seq": int(target_seq),
        "target_hash": target_hash,
        "annotation_id": annotation_id,
        "decision": decision,
        "label": label,
        "edited_label": edited_label,
    })


def find_entry(store, seq):
    for _, _, e in read_entries(store):
        if e and e.get("seq") == seq:
            return e
    return None


# ---------------------------------------------------------------------------
# CLI


def _flag(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            v = args[i + 1]
            del args[i:i + 2]
            return v
    return default


def _bool_flag(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


def _cli_store(args):
    return Store(_flag(args, "--project") or project_dir())


def main(argv):
    if not argv:
        sys.stderr.write(__doc__)
        return 2
    cmd, args = argv[0], list(argv[1:])

    if cmd in ("hook", "session-start"):
        # Hook path: never fail, never print (except the protocol).
        store = None
        try:
            raw = sys.stdin.read()
            if cmd == "session-start":
                cmd_session_start(raw)
            else:
                record_hook(json.loads(raw))
        except BaseException as exc:  # noqa: B902
            store = Store(project_dir())
            if store.exists():
                store.log_error("ledger.%s" % cmd, exc)
        return 0

    if cmd == "verify":
        as_json = _bool_flag(args, "--json")
        anchors = _bool_flag(args, "--anchors")
        store = _cli_store(args)
        res = verify(store)
        if anchors:
            import anchor

            res["anchors"] = anchor.verify_anchors(store)
            if not res["anchors"]["ok"]:
                res["ok"] = False
        if as_json:
            print(canonical_json(res))
        elif res["ok"]:
            print("ok: %d entries, head %s" % (res["entries"], (res["head"] or "-")[:16]))
        else:
            print("BROKEN at #%s: %s" % (res["broken_at"], res["reason"] or (res.get("anchors") or {}).get("reason")))
        return 0 if res["ok"] else 1

    if cmd == "status":
        store = _cli_store(args)
        print(canonical_json(verify(store, check_blobs=False)))
        return 0

    if cmd == "note":
        require_human("note")
        author = _flag(args, "--author")
        store = _cli_store(args)
        text = " ".join(args) if args else sys.stdin.read()
        if not text.strip():
            sys.stderr.write("note: empty text\n")
            return 2
        store.ensure()
        e = write_note(store, text, author=author)
        print("#%d %s" % (e["seq"], e["hash"][:12]))
        return 0

    if cmd == "confirm":
        require_human("confirm")
        store = _cli_store(args)
        seq = int(_flag(args, "--target-seq"))
        target = find_entry(store, seq)
        if not target:
            sys.stderr.write("confirm: no entry #%d\n" % seq)
            return 2
        e = write_confirm(store, seq, target["hash"], _flag(args, "--annotation-id"),
                          _flag(args, "--decision"), _flag(args, "--label"), _flag(args, "--edited-label"))
        print("#%d %s" % (e["seq"], e["hash"][:12]))
        return 0

    if cmd == "anchor":
        require_human("anchor")
        import anchor

        return anchor.main(args)

    sys.stderr.write("unknown command %r\n%s" % (cmd, __doc__))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
