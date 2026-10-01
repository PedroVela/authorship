import json
import os
import shutil
import time

import harness
import ledger
from harness import entries, run_hook

EXPECTED = [
    (1, "SessionStart", "system"),
    (2, "UserPromptSubmit", "human"),
    (3, "Stop", "ai"),
    (4, "UserPromptSubmit", "human"),
    (5, "PostToolUse", "ai"),
    (6, "PostToolUseFailure", "ai"),
    (7, "PostToolUse", "ai"),
    (8, "PostToolUse", "ai"),
    (9, "Stop", "ai"),
    (10, "ManualNote", "human"),
    (11, "UserPromptSubmit", "human"),
    (12, "PostToolUse", "ai"),
    (13, "UserPromptSubmit", "human"),
    (14, "SessionEnd", "system"),
]


def test_golden_fixture_replays_and_verifies(qr):
    es = entries(qr)
    assert [(e["seq"], e["event"], e["actor"]) for e in es] == EXPECTED
    res = ledger.verify(qr)
    assert res["ok"], res
    assert res["entries"] == 14
    assert es[1]["tags"] == ["#stage", "#problem"] and es[1]["stage"] == "Exploration"
    assert es[3]["tags"] == ["#idea"]
    assert es[9]["tags"] == ["#discard"]
    assert es[10]["stage"] == "Prototype" and es[10]["tags"] == ["#stage", "#decision"]
    assert es[12]["tags"] == ["#claim"]
    assert "3. TTL cache" in es[2]["text"] and es[2]["n_blocks"] == 1
    assert es[4]["file"] == "src/recon.py" and es[4]["file_sha_after"]
    assert es[5]["outcome"] == "failure" and "1 failed" in es[5]["error"]
    assert es[7]["outcome"] == "success" and es[7]["command"].startswith("pytest")
    assert es[13]["transcript"]["blob"]
    assert all(e["v"] == 2 for e in es)


def test_prototype_v1_ledger_verifies_unchanged(tmp_path):
    src = os.path.join(harness.FIXTURES, "v1_ledger", "ledger.jsonl")
    proj = tmp_path / "p"
    (proj / ".authorship").mkdir(parents=True)
    dst = proj / ".authorship" / "ledger.jsonl"
    shutil.copy(src, dst)
    before = dst.read_bytes()
    store = ledger.Store(str(proj))
    res = ledger.verify(store)
    assert res["ok"], res
    assert res["versions"] == {"1": 6}
    # v2 entries chain onto a v1 ledger; old bytes stay as they were.
    ledger.write_note(store, "#hipotesis sequence numbers are monotonic per account", author="inventor")
    res = ledger.verify(store)
    assert res["ok"] and res["versions"] == {"1": 6, "2": 1}
    assert dst.read_bytes().startswith(before)
    assert entries(store)[-1]["tags"] == ["#hypothesis"]


def _flip(line, pos):
    ch = line[pos]
    if ch.isdigit():
        repl = str((int(ch) + 1) % 10)
    elif ch.isalpha():
        repl = "b" if ch.lower() != "b" else "c"
    else:
        repl = "x"
    return line[:pos] + repl + line[pos + 1:]


def test_editing_one_byte_of_any_entry_breaks_at_that_seq(qr):
    path = qr.ledger
    with open(path, encoding="utf-8") as f:
        original = f.read().splitlines()
    for i, line in enumerate(original):
        candidates = [p for p, c in enumerate(line) if c.isalnum()]
        for pos in candidates[:: max(1, len(candidates) // 6)]:
            lines = list(original)
            lines[i] = _flip(line, pos)
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            res = ledger.verify(qr)
            assert not res["ok"] and res["broken_at"] == i + 1, (i, pos, line[pos], res)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(original) + "\n")
    assert ledger.verify(qr)["ok"]


def test_editing_one_byte_of_any_blob_breaks_at_first_referencing_seq(qr):
    first_ref = {}
    for e in entries(qr):
        for sha in ledger.iter_blob_refs(e):
            first_ref.setdefault(sha, e["seq"])
    assert len(first_ref) >= 8
    for sha, seq in first_ref.items():
        p = qr.blob_path(sha)
        data = open(p, "rb").read()
        tampered = bytearray(data)
        tampered[len(data) // 2] ^= 0x01
        open(p, "wb").write(bytes(tampered))
        res = ledger.verify(qr)
        assert not res["ok"] and res["broken_at"] == seq, (sha, res)
        open(p, "wb").write(data)
    assert ledger.verify(qr)["ok"]


def test_malformed_stdin_exits_zero_and_logs(project):
    harness.init_store(project)
    for bad in ("{not json", "", "[]", '{"hook_event_name": "PostToolUse", "tool_input": 5}'):
        r = run_hook(project, bad)
        assert r.returncode == 0 and r.stdout == ""
    log = open(os.path.join(project, ".authorship", "errors.log")).read()
    assert "ledger.hook" in log


def test_uninitialized_project_records_nothing(project):
    r = run_hook(project, {"hook_event_name": "UserPromptSubmit", "prompt": "hi", "cwd": project})
    assert r.returncode == 0
    assert not os.path.exists(os.path.join(project, ".authorship"))


def test_hook_latency_p95_under_100ms(project):
    harness.init_store(project)
    payload = json.dumps({
        "hook_event_name": "PostToolUse", "session_id": "lat", "cwd": project, "tool_name": "Bash",
        "tool_input": {"command": "echo hi"}, "tool_response": {"stdout": "hi", "stderr": ""},
    })
    env = harness.hook_env(project)
    times = []
    for _ in range(200):
        t = time.perf_counter()
        r = run_hook(project, payload, env=env)
        times.append(time.perf_counter() - t)
        assert r.returncode == 0
    times.sort()
    p95 = times[int(len(times) * 0.95)]
    print("hook latency median %.1f ms, p95 %.1f ms" % (times[100] * 1000, p95 * 1000))
    assert p95 < 0.100
    assert ledger.verify(ledger.Store(project))["entries"] == 200


def _stop(project, transcript, **kw):
    p = {"hook_event_name": "Stop", "session_id": "s", "cwd": project, "transcript_path": transcript}
    p.update(kw)
    assert run_hook(project, p).returncode == 0


def _say(transcript, text):
    with open(transcript, "a") as f:
        f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n")


def test_stop_does_not_duplicate_on_repeated_calls(project):
    store = harness.init_store(project)
    t = os.path.join(project, "t.jsonl")
    _say(t, "first answer")
    _stop(project, t)
    _stop(project, t)
    _stop(project, t, last_assistant_message="first answer")
    es = [e for e in entries(store) if e["event"] == "Stop"]
    assert [e["n_blocks"] for e in es] == [1, 0, 0]
    assert es[0]["text"] == "first answer" and es[1]["text"] == ""


def test_interrupted_turn_text_is_picked_up_at_next_stop(project):
    store = harness.init_store(project)
    t = os.path.join(project, "t.jsonl")
    _say(t, "turn one")
    _stop(project, t)
    _say(t, "turn two, interrupted before Stop fired")
    # a partial line (still being written) must not be consumed
    with open(t, "a") as f:
        f.write('{"type": "assistant", "message": {"content": [{"type": "text", "text": "tur')
    _stop(project, t)
    with open(t, "a") as f:
        f.write('n three"}]}}\n')
    _stop(project, t)
    es = [e for e in entries(store) if e["event"] == "Stop"]
    assert es[1]["text"] == "turn two, interrupted before Stop fired"
    assert es[2]["text"] == "turn three"


def test_stop_falls_back_to_last_assistant_message(project):
    store = harness.init_store(project)
    _stop(project, os.path.join(project, "missing.jsonl"), last_assistant_message="from payload")
    assert entries(store)[-1]["text"] == "from payload"


def test_large_text_goes_to_blob_with_preview(project):
    store = harness.init_store(project)
    big = "x" * 9000
    run_hook(project, {"hook_event_name": "UserPromptSubmit", "prompt": "#idea " + big, "cwd": project})
    e = entries(store)[-1]
    assert "text" not in e and len(e["preview"]) == 400 and e["bytes"] == 9006 and e["blob"] == e["sha256"]
    assert ledger.verify(store)["ok"]


def test_skip_tools_and_own_mcp_calls_recorded(project):
    store = harness.init_store(project)
    for tool in ("Read", "Glob", "mcp__plugin_authorship_authorship__search"):
        run_hook(project, {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": {}, "cwd": project})
    assert [e["tool"] for e in entries(store)] == ["mcp__plugin_authorship_authorship__search"]


def test_secrets_redacted_before_hashing(project):
    store = harness.init_store(project)
    key = "sk-ant-api03-" + "A" * 40
    run_hook(project, {"hook_event_name": "UserPromptSubmit", "prompt": "use " + key, "cwd": project})
    run_hook(project, {"hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": project,
                       "tool_input": {"command": "export ANTHROPIC_API_KEY=" + key}, "tool_response": {"stdout": key}})
    raw = open(store.ledger).read()
    for root, _, files in os.walk(store.blobs):
        for n in files:
            raw += open(os.path.join(root, n)).read()
    assert key not in raw and "[REDACTED:anthropic]" in raw


def test_tag_aliases_normalize_to_english():
    tags, stage = ledger.parse_tags("#etapa Exploración #problema lento #hipotesis x #descarte y #idea z #claim w #foo")
    assert tags == ["#stage", "#problem", "#hypothesis", "#discard", "#idea", "#claim"]
    assert stage == "Exploración"
    assert ledger.parse_tags('#stage "Proof of concept" go')[1] == "Proof of concept"
    assert ledger.parse_tags("see #13 and http://x/#idea")[0] == []


def test_torn_tail_is_repaired_without_touching_complete_entries(project):
    store = harness.init_store(project)
    ledger.write_note(store, "one")
    good = open(store.ledger, "rb").read()
    with open(store.ledger, "ab") as f:
        f.write(b'{"v":2,"seq":2,"ts"')
    ledger.write_note(store, "two")
    assert open(store.ledger, "rb").read().startswith(good)
    assert ledger.verify(store)["ok"] and ledger.verify(store)["entries"] == 2
    assert "torn-tail" in open(store.errors).read()


def test_stop_takes_unflushed_final_message_from_payload_once(project):
    store = harness.init_store(project)
    t = os.path.join(project, "t.jsonl")
    _say(t, "working on it")
    _stop(project, t, last_assistant_message="all done")  # final text not in the transcript yet
    _say(t, "all done")
    _stop(project, t)
    es = [e for e in entries(store) if e["event"] == "Stop"]
    assert es[0]["text"] == "working on it\n\nall done" and es[0]["n_blocks"] == 2
    assert es[1]["n_blocks"] == 0


def test_verify_blob_cache_still_catches_tampering(qr):
    cache = {}
    assert ledger.verify(qr, blob_cache=cache)["ok"] and cache
    e = entries(qr)[4]
    p = qr.blob_path(e["input"]["blob"])
    data = open(p, "rb").read()
    time.sleep(0.01)
    open(p, "wb").write(data[:-1] + b"X")
    res = ledger.verify(qr, blob_cache=cache)
    assert not res["ok"] and res["broken_at"] == 5


def test_init_hint_once_per_git_project(project, tmp_path):
    env = harness.hook_env(project, CLAUDE_PLUGIN_DATA=str(tmp_path / "data"), AUTHORSHIP_HINT="1")
    start = {"hook_event_name": "SessionStart", "session_id": "h", "cwd": project, "source": "startup"}
    assert run_hook(project, start, "session-start", env=env).stdout == ""  # not a git repo: silent
    os.makedirs(os.path.join(project, ".git"))
    open(os.path.join(project, ".git", "HEAD"), "w").write("ref: refs/heads/main\n")
    first = run_hook(project, start, "session-start", env=env)
    assert first.returncode == 0 and "/authorship:init" in first.stdout and "Do not run it yourself" in first.stdout
    assert run_hook(project, start, "session-start", env=env).stdout == ""  # only once
    assert not os.path.exists(os.path.join(project, ".authorship"))
    other = str(tmp_path / "other")
    os.makedirs(os.path.join(other, ".git"))
    open(os.path.join(other, ".git", "HEAD"), "w").write("ref: refs/heads/main\n")
    off = harness.hook_env(other, CLAUDE_PLUGIN_DATA=str(tmp_path / "data"), AUTHORSHIP_HINT="0")
    assert run_hook(other, dict(start, cwd=other), "session-start", env=off).stdout == ""
