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


@pytest.mark.skipif(os.name == "nt", reason="the wrapper is a sh script; Windows runs it through Git Bash, not directly")
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
    assert "Installed your command" in r.stdout and "Setup check:" in r.stdout
    assert os.path.exists(os.path.join(project, ".test-bin", "authorship"))


# --- authorship review -------------------------------------------------------------------

import io
import json

import annotator
import index


def with_suggestions(store):
    annotator.run_once(store)
    with open(store.annotations, "a") as f:
        f.write(json.dumps({"id": "ann_t1", "ts": "2099-01-01T00:00:00.000Z", "target_seq": 4, "model": "jev-1.13.0",
                            "questions_hash": "q", "answers": {}, "supersedes": None,
                            "milestones": [{"type": "conception_candidate", "tier": 1, "score": 0.83}],
                            "edges": [{"src": "4", "dst": "3.3", "type": "modifies", "p": 0.81}]}) + "\n")
        f.write(json.dumps({"id": "ann_t2", "ts": "2099-01-01T00:00:00.000Z", "target_seq": 11, "model": "jev-1.13.0",
                            "questions_hash": "q", "answers": {}, "supersedes": None, "edges": [],
                            "milestones": [{"type": "ai_origin_element", "tier": 1, "score": 0.6}]}) + "\n")
    return store


def test_review_list_puts_unfavorable_first(qr):
    with_suggestions(qr)
    out = io.StringIO()
    assert cli.cmd_review(qr, ["--list"], stdout=out) == 0
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("#11") and "AI-origin element" in lines[0] and "0.60" in lines[0]
    assert any("#4 modifies #3.3" in l and "0.81" in l for l in lines)
    assert any("conception moment" in l for l in lines)


def test_review_accept_reject_edit_write_confirm_entries(qr):
    with_suggestions(qr)
    before = len(entries(qr))
    # order: #11 ai_origin (accept), #4 edge (reject), #4 conception (edit -> maturity jump)
    answers = "a\nr\ne\nmilestone:maturity_jump\n"
    out = io.StringIO()
    assert cli.cmd_review(qr, [], stdin=io.StringIO(answers), stdout=out) == 0
    new = entries(qr)[before:]
    assert [(e["event"], e["actor"], e["decision"]) for e in new] == [
        ("Confirm", "human", "accept"), ("Confirm", "human", "reject"), ("Confirm", "human", "edit")]
    assert new[2]["edited_label"] == "milestone:maturity_jump" and new[1]["label"] == "edge:4:modifies:3.3"
    assert "3 decision(s) recorded" in out.getvalue()
    assert ledger.verify(qr)["ok"]
    conn = index.update(qr)
    assert index.review_queue(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM edges WHERE src='4' AND dst='3.3' AND source='annotation'").fetchone()[0] == 0


def test_review_skip_quit_and_bad_edit_write_nothing(qr):
    with_suggestions(qr)
    before = len(entries(qr))
    out = io.StringIO()
    cli.cmd_review(qr, [], stdin=io.StringIO("x\ns\ne\nnot a label\nq\n"), stdout=out)
    assert len(entries(qr)) == before
    assert "a, r, e, s or q" in out.getvalue() and "not a valid label" in out.getvalue()


def test_review_empty_queue(qr):
    out = io.StringIO()
    assert cli.cmd_review(qr, [], stdin=io.StringIO(""), stdout=out) == 0 and "Nothing to review" in out.getvalue()


def test_review_refuses_claude(qr):
    r = run_cli(["review", "--list"], cwd=qr.project, CLAUDECODE="1")
    assert r.returncode == 3 and "human-only" in r.stderr


def test_classifier_command_explains_setup(qr, monkeypatch):
    import io
    monkeypatch.setenv("AUTHORSHIP_AUTO", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    out = io.StringIO()
    assert cli.cmd_classifier(qr, [], stdout=out) == 0
    text = out.getvalue()
    assert "This terminal would use: Jev (jev-1.13.0); entry text goes to TypeSafe AI" in text
    assert "export TYPESAFE_API_KEY" in text and "authorship restart" in text and "AUTHORSHIP_AUTO=0" in text
    monkeypatch.setenv("AUTHORSHIP_AUTO", "0")
    out = io.StringIO()
    cli.cmd_classifier(qr, [], stdout=out)
    assert "This terminal would use: off (AUTHORSHIP_AUTO=0)" in out.getvalue()


@pytest.mark.parametrize("cmd", [["restart"], ["classifier", "--test"]])
def test_restart_and_test_refuse_claude(qr, cmd):
    r = run_cli(cmd, cwd=qr.project, CLAUDECODE="1")
    assert r.returncode == 3 and "human-only" in r.stderr


# --- doctor ---------------------------------------------------------------------------------------


def test_doctor_reports_each_piece(qr, monkeypatch, tmp_path):
    import io
    monkeypatch.setenv("AUTHORSHIP_OTS_HOME", str(tmp_path / "ots"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    checks = {c[1]: c for c in cli.doctor_checks(qr)}
    assert checks["Python"][0] == "ok"
    assert checks["authorship command"][0] == "fix" and checks["authorship command"][2] == "not installed"
    assert checks["Bitcoin timestamps (OpenTimestamps)"][0] == "fix"
    assert checks["Record"][0] == "ok" and "14 entries, intact" in checks["Record"][2]
    assert checks["Protection rules"][0] == "todo"  # the fixture project never ran init
    assert checks["Background annotator and viewer"][0] == "fix"
    out = io.StringIO()
    cli.print_checks(cli.doctor_checks(qr), out)
    assert "doctor --fix installs ~/.local/bin/authorship" in out.getvalue()


@pytest.mark.skipif(os.name == "nt", reason="checks a POSIX shell profile and an ots symlink")
def test_doctor_fix_installs_command_path_ots_and_daemons(qr, monkeypatch, tmp_path):
    import io
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(ledger, "require_human", lambda what: None)  # the test runner runs under Claude Code
    started = []
    monkeypatch.setattr(ledger, "spawn_detached", lambda argv, env=None: started.append(os.path.basename(argv[1])))
    monkeypatch.setattr(cli, "install_ots", lambda: str(tmp_path / "bin" / "ots"))
    out = io.StringIO()
    assert cli.cmd_doctor(["--fix", "--project", qr.project], stdout=out) == 0
    text = out.getvalue()
    assert os.path.exists(os.path.join(os.environ["AUTHORSHIP_BIN_DIR"], "authorship"))
    assert cli.PATH_LINE in open(str(home / ".zshrc")).read() and "open a new terminal" in text
    assert "fixed: started the annotator and the viewer" in text and started[:2] == ["annotator.py", "viewer.py"]
    assert "fixed: " + str(tmp_path / "bin" / "ots") in text
    cli.cmd_doctor(["--fix", "--project", qr.project], stdout=io.StringIO())
    assert open(str(home / ".zshrc")).read().count(cli.PATH_LINE) == 1  # idempotent


def test_doctor_fix_refuses_claude(qr):
    r = run_cli(["doctor", "--fix"], cwd=qr.project, CLAUDECODE="1")
    assert r.returncode == 3 and "human-only" in r.stderr
    r = run_cli(["doctor"], cwd=qr.project, CLAUDECODE="1")
    assert r.returncode == 0 and "authorship doctor" in r.stdout


def test_anchor_finds_ots_outside_the_path(monkeypatch, tmp_path):
    import anchor
    b = tmp_path / "bin"
    b.mkdir()
    f = b / "ots"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("AUTHORSHIP_BIN_DIR", str(b))
    assert anchor.ots_bin() == str(f)
