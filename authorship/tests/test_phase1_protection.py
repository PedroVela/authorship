import json
import os
import subprocess
import sys

import pytest

import harness
import init_project
import ledger
from harness import PLUGIN, SCRIPTS, entries

B, A, ASK = "block", "allow", "ask"
PY = "python3 %s/%%s" % SCRIPTS

CASES = [
    # file tools
    (B, "Edit", {"file_path": ".authorship/ledger.jsonl"}),
    (B, "Write", {"file_path": "{project}/.authorship/blobs/ab/x"}),
    (B, "Edit", {"file_path": "../proj/.authorship/head.json"}),
    (B, "Edit", {"file_path": "notes"}),  # symlink to the ledger
    (B, "Write", {"file_path": "store_link/state.json"}),  # symlinked directory
    (B, "MultiEdit", {"file_path": "src/../.authorship/state.json"}),
    (B, "NotebookEdit", {"notebook_path": ".authorship/x.ipynb"}),
    (B, "Read", {"file_path": ".authorship/run/viewer.secret"}),
    (B, "Glob", {"pattern": ".authorship/run/*"}),
    # bash writes into the store
    (B, "Bash", {"command": "sed -i '' 's/human/ai/' .authorship/ledger.jsonl"}),
    (B, "Bash", {"command": "echo '{}' > .authorship/ledger.jsonl"}),
    (B, "Bash", {"command": "echo x | tee -a .authorship/ledger.jsonl"}),
    (B, "Bash", {"command": "mv .authorship /tmp/elsewhere"}),
    (B, "Bash", {"command": "rm -rf .authorship"}),
    (B, "Bash", {"command": "rm -rf .a*"}),
    (B, "Bash", {"command": "rm -rf ./.[a]uthorship"}),
    (B, "Bash", {"command": "rm -rf ./.??*"}),
    (B, "Bash", {"command": "rm -rf ../proj"}),
    (B, "Bash", {"command": "cd .. && rm -rf proj"}),
    (B, "Bash", {"command": "rm -rf ."}),
    (B, "Bash", {"command": "find . -delete"}),
    (B, "Bash", {"command": "find . -name 'ledger.jsonl' -delete"}),
    (B, "Bash", {"command": "python -c \"open('.authorship/ledger.jsonl','w')\""}),
    (B, "Bash", {"command": "python3 -c \"import os; os.remove('.author' + 'ship/head.json')\""}),
    (B, "Bash", {"command": "printf x > store_link/ledger.jsonl"}),
    (B, "Bash", {"command": "ln -sf /dev/null notes"}),
    (B, "Bash", {"command": "cat $'\\x2eauthorship/ledger.jsonl'"}),
    (B, "Bash", {"command": "cat .authorship/run/viewer.secret"}),
    (B, "Bash", {"command": "python3 -c \"import sys; sys.path.insert(0, 's'); import ledger; ledger.write_note(0, 'x')\""}),
    # human-only CLI and viewer
    (B, "Bash", {"command": PY % "ledger.py note '#idea I invented it'"}),
    (B, "Bash", {"command": PY % "ledger.py confirm --target-seq 1 --decision accept"}),
    (B, "Bash", {"command": PY % "anchor.py"}),
    (B, "Bash", {"command": "cd %s && python3 ledger.py anchor" % SCRIPTS}),
    (B, "Bash", {"command": PY % "viewer.py daemon"}),
    (B, "Bash", {"command": "authorship note '#idea mine, honestly'"}),
    (B, "Bash", {"command": "cd src && authorship --project .. seal"}),
    (B, "Bash", {"command": PY % "cli.py open"}),
    (B, "Bash", {"command": "curl -X POST http://127.0.0.1:47291/api/confirm -d '{}'"}),
    (B, "Bash", {"command": "python3 -c \"import urllib.request as u; u.urlopen('http://localhost:47291/api/graph')\""}),
    # git history rewrites
    (B, "Bash", {"command": "git filter-repo --force"}),
    (B, "Bash", {"command": "git filter-branch --tree-filter 'true' HEAD"}),
    (B, "Bash", {"command": "git reset --hard"}),
    (B, "Bash", {"command": "git reset --hard HEAD~1"}),
    (B, "Bash", {"command": "git checkout -- ."}),
    (B, "Bash", {"command": "git restore ."}),
    (B, "Bash", {"command": "git stash"}),
    (B, "Bash", {"command": "git clean -fdx"}),
    (B, "Bash", {"command": "git commit --amend -m 'tidy'"}),
    (B, "Bash", {"command": "git rebase -i --root"}),
    # normal work
    (A, "Edit", {"file_path": "src/app.py"}),
    (A, "Write", {"file_path": "{project}/README.md"}),
    (A, "Read", {"file_path": "src/app.py"}),
    (A, "Read", {"file_path": ".authorship/ledger.jsonl"}),
    (A, "Edit", {"file_path": ".claude/settings.json"}),  # handled by the permission `ask` rule
    (A, "Bash", {"command": "pytest -q"}),
    (A, "Bash", {"command": "npm test"}),
    (A, "Bash", {"command": "git status"}),
    (A, "Bash", {"command": "git add src/app.py && git commit -m 'Add feature'"}),
    (A, "Bash", {"command": "git log --oneline -5 && git diff"}),
    (A, "Bash", {"command": "git checkout -- src/app.py"}),
    (A, "Bash", {"command": "git restore --staged src/app.py"}),
    (A, "Bash", {"command": "git checkout -b feature"}),
    (A, "Bash", {"command": "git stash list"}),
    (A, "Bash", {"command": "rm -rf build dist node_modules"}),
    (A, "Bash", {"command": "rm -rf *"}),
    (A, "Bash", {"command": "mv src/app.py src/main.py"}),
    (A, "Bash", {"command": "mv src/app.py ."}),
    (A, "Bash", {"command": "find . -name '*.pyc' -delete"}),
    (A, "Bash", {"command": "sed -i '' 's/a/b/' src/app.py"}),
    (A, "Bash", {"command": "echo hi > out.txt && tee log.txt < out.txt"}),
    (A, "Bash", {"command": "python -c 'print(1)'"}),
    (A, "Bash", {"command": "curl -s https://example.com"}),
    (A, "Bash", {"command": "curl http://127.0.0.1:3000/health"}),
    (A, "Bash", {"command": "grep -rn foo src"}),
    (A, "Bash", {"command": "cd src && rm -rf __pycache__"}),
    (A, "Bash", {"command": "ls -la"}),
    (A, "Bash", {"command": "echo 'the authorship plugin'"}),
    (A, "Bash", {"command": PY % "anchor.py seal"}),
    (A, "Bash", {"command": PY % "index.py --rebuild"}),
    (A, "Bash", {"command": PY % "init_project.py"}),
    (A, "Bash", {"command": "authorship log -n 5 && authorship status"}),
    (A, "Bash", {"command": "authorship verify"}),
    # ask the human
    (ASK, "Bash", {"command": "cat .claude/settings.json"}),
    (ASK, "Bash", {"command": "claude plugin disable authorship"}),
]


def git(project, *args):
    subprocess.run(["git", "-C", project] + list(args), check=True, capture_output=True)


@pytest.fixture(scope="module")
def guarded(tmp_path_factory):
    project = str(tmp_path_factory.mktemp("g") / "proj")
    os.makedirs(os.path.join(project, "src"))
    open(os.path.join(project, "src", "app.py"), "w").write("print('hi')\n")
    git(project, "init", "-q")
    git(project, "config", "user.email", "t@example.com")
    git(project, "config", "user.name", "t")
    init_project.init(project)
    store = ledger.Store(project)
    ledger.write_note(store, "#idea first")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "first")
    ledger.write_note(store, "#idea second")  # uncommitted ledger line
    os.symlink(os.path.join(project, ".authorship", "ledger.jsonl"), os.path.join(project, "notes"))
    os.symlink(os.path.join(project, ".authorship"), os.path.join(project, "store_link"))
    return project


def run_guard(project, tool, tin, cwd=None):
    tin = json.loads(json.dumps(tin).replace("{project}", project))
    payload = {"hook_event_name": "PreToolUse", "session_id": "g", "cwd": cwd or project,
               "tool_name": tool, "tool_input": tin}
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, "guard.py")], input=json.dumps(payload),
                          capture_output=True, text=True, env=harness.hook_env(project), timeout=20)


def test_case_table_is_big_enough():
    assert sum(1 for c in CASES if c[0] == B) >= 30
    assert sum(1 for c in CASES if c[0] == A) >= 20


@pytest.mark.parametrize("expect,tool,tin", CASES, ids=["%s-%s-%s" % (c[0], c[1], list(c[2].values())[0][:40]) for c in CASES])
def test_guard_case(guarded, expect, tool, tin):
    before = len(entries(ledger.Store(guarded)))
    r = run_guard(guarded, tool, tin)
    after = entries(ledger.Store(guarded))
    if expect == B:
        assert r.returncode == 2, (r.stdout, r.stderr)
        assert "authorship guard: blocked" in r.stderr
        assert len(after) == before + 1 and after[-1]["event"] == "GuardBlock" and after[-1]["tool"] == tool
    elif expect == A:
        assert r.returncode == 0 and r.stdout == "", (r.stdout, r.stderr)
        assert len(after) == before
    else:
        assert r.returncode == 0
        assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert ledger.verify(ledger.Store(guarded))["ok"]


def test_guard_allows_everything_without_a_store(project):
    r = run_guard(project, "Bash", {"command": "rm -rf .authorship"})
    assert r.returncode == 0


def test_guard_block_redacts_the_command(guarded):
    run_guard(guarded, "Bash", {"command": "echo sk-ant-api03-%s > .authorship/x" % ("B" * 40)})
    e = entries(ledger.Store(guarded))[-1]
    assert e["event"] == "GuardBlock" and "[REDACTED:anthropic]" in e["command_or_path"]


def test_guard_internal_error_allows_and_logs(project):
    harness.init_store(project)
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "guard.py")], input="{broken",
                       capture_output=True, text=True, env=harness.hook_env(project))
    assert r.returncode == 0
    assert "guard" in open(os.path.join(project, ".authorship", "errors.log")).read()


# --- init -------------------------------------------------------------------


def test_init_is_idempotent_and_merges(project):
    settings = os.path.join(project, ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings))
    existing = {"model": "opus", "hooks": {"Stop": []}, "permissions": {"allow": ["Bash(npm test)"], "deny": ["Read(.env)"]}}
    json.dump(existing, open(settings, "w"))
    open(os.path.join(project, ".gitignore"), "w").write("node_modules/\n")
    r1 = init_project.init(project)
    assert r1["created"] and len(r1["rules_added"]) == 4
    first = open(settings).read()
    r2 = init_project.init(project)
    assert not r2["created"] and r2["rules_added"] == [] and r2["gitignore_added"] == []
    assert open(settings).read() == first
    data = json.loads(first)
    assert data["model"] == "opus" and data["hooks"] == {"Stop": []}
    assert data["permissions"]["allow"] == ["Bash(npm test)"]
    assert data["permissions"]["deny"] == ["Read(.env)", "Edit(/.authorship/**)", "Read(/.authorship/run/**)"]
    assert data["permissions"]["ask"] == ["Edit(/.claude/settings.json)", "Edit(/.claude/settings.local.json)"]
    gi = open(os.path.join(project, ".gitignore")).read().splitlines()
    assert gi[0] == "node_modules/" and ".authorship/index.sqlite" in gi and ".authorship/run/" in gi
    assert gi.count(".authorship/run/") == 1
    assert os.path.isdir(os.path.join(project, ".authorship", "blobs"))


def test_init_cli_prints_statusline_snippet(project):
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "init_project.py"), "--project", project],
                       capture_output=True, text=True, env=harness.hook_env(project))
    assert r.returncode == 0 and '"statusLine"' in r.stdout and "sandbox" in r.stdout
    protocol = open(os.path.join(SCRIPTS, "protocol.md")).read().rstrip()
    assert r.stdout.rstrip().endswith(protocol) and "no restart needed" in r.stdout


def test_init_starts_daemons_without_restart(project, monkeypatch):
    started = []
    monkeypatch.delenv("AUTHORSHIP_NO_DAEMONS", raising=False)
    monkeypatch.setattr(ledger, "spawn_detached", lambda argv, env=None: started.append(os.path.basename(argv[1])))
    init_project.init(project)
    text = init_project.activate(project)
    assert started == ["annotator.py", "viewer.py"]
    assert text.startswith("This project records authorship for patent purposes.")
    monkeypatch.setenv("AUTHORSHIP_NO_DAEMONS", "1")
    init_project.activate(project)
    assert len(started) == 2


# --- human-only CLI --------------------------------------------------------


def _cli(project, *args, env=None):
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, "ledger.py")] + list(args) + ["--project", project],
                          capture_output=True, text=True, env=env, timeout=20)


@pytest.mark.parametrize("args", [["note", "#idea mine"], ["confirm", "--target-seq", "1", "--decision", "accept"], ["anchor"]])
def test_human_only_cli_refuses_claude_env_marker(project, args):
    harness.init_store(project)
    r = _cli(project, *args, env=harness.hook_env(project, CLAUDECODE="1"))
    assert r.returncode == 3 and "human-only" in r.stderr
    assert entries(ledger.Store(project)) == []


def test_human_only_cli_refuses_claude_parent_process(project, tmp_path):
    """Second signal: an ancestor process named `claude`, even without the env marker."""
    harness.init_store(project)
    fake = str(tmp_path / "claude")
    os.symlink("/bin/sh", fake)  # a copied system binary is killed by code signing; ps reports the link name
    script = "%s %s note '#idea forged' --project %s; true" % (sys.executable, os.path.join(SCRIPTS, "ledger.py"), project)
    r = subprocess.run([fake, "-c", script], capture_output=True, text=True, env=harness.hook_env(project), timeout=20)
    assert "human-only" in r.stderr
    assert entries(ledger.Store(project)) == []


def test_spawned_by_claude_signals():
    assert ledger.spawned_by_claude({"CLAUDECODE": "1"}, check_parents=False)
    assert ledger.spawned_by_claude({"CLAUDE_CODE_ENTRYPOINT": "cli"}, check_parents=False)
    assert not ledger.spawned_by_claude({}, check_parents=False)
