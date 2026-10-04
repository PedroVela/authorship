"""Replay hook-event scripts through `ledger.py hook`, as Claude Code would."""
import json
import os
import subprocess
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(TESTS)
SCRIPTS = os.path.join(PLUGIN, "scripts")
FIXTURES = os.path.join(TESTS, "fixtures")
sys.path.insert(0, SCRIPTS)

import ledger  # noqa: E402


def hook_env(project, **extra):
    env = dict(os.environ)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PROJECT_DIR"):
        env.pop(k, None)
    env["AUTHORSHIP_PROJECT_DIR"] = project
    env["AUTHORSHIP_NO_DAEMONS"] = "1"
    env["AUTHORSHIP_AUTO"] = "0"  # never call the real classifier from tests
    env["AUTHORSHIP_ANCHOR"] = "0"  # never contact a real timestamp authority at session end
    env["AUTHORSHIP_HINT"] = "0"
    env["AUTHORSHIP_BIN_DIR"] = os.path.join(project if os.path.isdir(project) else "/tmp", ".test-bin")  # never ~/.local/bin
    env["CLAUDE_PLUGIN_ROOT"] = PLUGIN
    env.update(extra)
    return env


def run_hook(project, payload, command="hook", env=None):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "ledger.py"), command],
        input=raw, capture_output=True, text=True, env=env or hook_env(project), timeout=30,
    )


def _subst(obj, mapping):
    if isinstance(obj, str):
        for k, v in mapping.items():
            obj = obj.replace("{%s}" % k, v)
        return obj
    if isinstance(obj, list):
        return [_subst(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: _subst(v, mapping) for k, v in obj.items()}
    return obj


def init_store(project):
    store = ledger.Store(project)
    store.ensure()
    return store


def replay(project, name="qr_session", on_step=None):
    """Replay fixtures/<name>/events.json into project. Returns the Store."""
    with open(os.path.join(FIXTURES, name, "events.json"), encoding="utf-8") as f:
        steps = json.load(f)["steps"]
    store = init_store(project)
    transcript = os.path.join(project, "transcript.jsonl")
    mapping = {"project": project, "transcript": transcript}
    for step in _subst(steps, mapping):
        if "transcript" in step:
            with open(transcript, "a", encoding="utf-8") as f:
                for line in step["transcript"]:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
        elif "write_file" in step:
            path = os.path.join(project, step["write_file"]["path"])
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(step["write_file"]["content"])
        elif "note" in step:
            ledger.write_note(store, step["note"]["text"], author=step["note"].get("author"))
        elif "hook" in step:
            payload = step["hook"]
            cmd = "session-start" if payload["hook_event_name"] == "SessionStart" else "hook"
            r = run_hook(project, payload, cmd)
            assert r.returncode == 0, r.stderr
        if on_step:
            on_step(store)
    return store


def entries(store):
    return [e for _, _, e in ledger.read_entries(store) if e]


def load_edges(name="qr_session"):
    with open(os.path.join(FIXTURES, name, "edges.json"), encoding="utf-8") as f:
        return json.load(f)["edges"]


def confirm_edges(store, edges):
    """Record each edge as a human confirmation, the way the viewer does."""
    for e in edges:
        target = ledger.find_entry(store, int(str(e["src"]).split(".")[0]))
        ledger.write_confirm(store, target["seq"], target["hash"], None, "accept",
                             "edge:%s:%s:%s" % (e["src"], e["type"], e["dst"]))
