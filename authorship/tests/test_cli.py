import os
import subprocess
import sys

import pytest

import cli
import harness
import ledger
from harness import SCRIPTS, entries


def run_cli(args, cwd, **env):
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, "cli.py")] + args, cwd=cwd, capture_output=True,
                          text=True, env=harness.hook_env(cwd, **env) if env else _env(), timeout=60)


def _env():
    env = harness.hook_env("/nonexistent")
    env.pop("AUTHORSHIP_PROJECT_DIR")
    return env


def test_finds_project_from_a_subdirectory(qr):
    sub = os.path.join(qr.project, "src", "deep")
    os.makedirs(sub)
    assert cli.find_project(sub) == qr.project
    assert cli.find_project("/") is None


def test_log_shows_human_entries_and_uses_parent_project(qr):
    r = run_cli(["log"], cwd=os.path.join(qr.project, "src"))
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0].startswith("#2 ") and "you" in lines[0] and "prompt" in lines[0] and "#problem" in lines[0]
    assert any(l.startswith("#10 ") and "note" in l and "#discard Bloom filter" in l for l in lines)
    assert not any(" tool " in l for l in lines)
    r = run_cli(["log", "--all", "-n", "3"], cwd=qr.project)
    assert [l.split()[0] for l in r.stdout.splitlines()] == ["#12", "#13", "#14"]


def test_verify_and_status(qr):
    r = run_cli(["verify"], cwd=qr.project)
    assert r.returncode == 0 and r.stdout.startswith("ok: 14 entries")
    r = run_cli(["status"], cwd=qr.project)
    assert r.returncode == 0 and r.stdout.startswith("authorship ✓ 14 | 14 unsealed")


def test_outside_a_project_explains_what_to_do(tmp_path):
    r = run_cli(["log"], cwd=str(tmp_path))
    assert r.returncode == 1 and "authorship init" in r.stderr


@pytest.mark.parametrize("cmd", [["note", "#idea x"], ["seal"], ["open"]])
def test_human_only_commands_refuse_claude(qr, cmd):
    before = len(entries(qr))
    r = run_cli(cmd, cwd=qr.project, CLAUDECODE="1")
    assert r.returncode == 3 and "human-only" in r.stderr
    assert len(entries(qr)) == before


def test_note_command_writes_a_human_note(qr, monkeypatch, capsys):
    monkeypatch.setattr(ledger, "require_human", lambda what: None)  # the test runner itself runs under Claude Code
    monkeypatch.chdir(os.path.join(qr.project, "src"))
    assert cli.main(["note", "#hipotesis", "counters", "never", "go", "back"]) == 0
    e = entries(qr)[-1]
    assert e["event"] == "ManualNote" and e["actor"] == "human" and e["tags"] == ["#hypothesis"]
    assert e["text"] == "#hipotesis counters never go back"
    assert capsys.readouterr().out.startswith("#15 ")


def test_install_writes_a_working_wrapper(qr, tmp_path):
    bin_dir = str(tmp_path / "bin")
    r = run_cli(["install", "--bin-dir", bin_dir], cwd=str(tmp_path))
    assert r.returncode == 0 and "installed" in r.stdout and "not on your PATH" in r.stdout
    wrapper = os.path.join(bin_dir, "authorship")
    assert os.access(wrapper, os.X_OK)
    out = subprocess.run([wrapper, "verify"], cwd=qr.project, capture_output=True, text=True, env=_env())
    assert out.returncode == 0 and out.stdout.startswith("ok: 14 entries")


def test_init_via_cli_and_hint(project):
    r = run_cli(["init"], cwd=project, AUTHORSHIP_NO_DAEMONS="1")
    assert r.returncode == 0 and os.path.isdir(os.path.join(project, ".authorship"))
    assert "cli.py\" install" in r.stdout
