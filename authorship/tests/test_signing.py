import io
import json
import os
import shutil
import subprocess

import pytest

import cli
import harness
import ledger
import signing
from harness import entries

pytestmark = pytest.mark.skipif(not signing.ssh_keygen(), reason="ssh-keygen not installed")


def make_key(tmp_path, name="id_ed25519"):
    key = str(tmp_path / name)
    subprocess.run([signing.ssh_keygen(), "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", key], check=True,
                   capture_output=True)
    if os.name == "nt":  # Windows OpenSSH refuses a key others can read, as in ~/.ssh
        subprocess.run(["icacls", key, "/inheritance:r", "/grant:r", "%s:F" % os.environ["USERNAME"]], check=True,
                       capture_output=True)
    return key


@pytest.fixture
def signer(tmp_path):
    return signing.setup(make_key(tmp_path), "inventor@example.com")


def test_human_entries_are_signed_inside_the_chain(project, signer):
    harness.init_store(project)
    store = ledger.Store(project)
    ledger.write_note(store, "#idea contador monótono del extracto")
    ledger.append(store, "Stop", "ai", "s", {"text": "ok"})
    es = entries(store)
    note, ai = es[0], es[1]
    assert note["sig"]["key"] == signer["fingerprint"] and note["sig"]["principal"] == "inventor@example.com"
    assert note["sig"]["sshsig"].startswith("-----BEGIN SSH SIGNATURE-----")
    assert "sig" not in ai  # only the human's entries
    rep = signing.verify(store)
    assert rep == {"human": 1, "signed": 1, "valid": 1, "invalid": [], "unchecked": 0,
                   "keys": {signer["fingerprint"]: "inventor@example.com"}, "ok": True}
    assert ledger.verify(store)["ok"]
    assert "1 of 1 human entries signed" in signing.describe(rep) and "all valid" in signing.describe(rep)


def test_prompts_from_the_hook_are_signed(project, signer):
    harness.init_store(project)
    r = harness.run_hook(project, {"hook_event_name": "UserPromptSubmit", "session_id": "s", "cwd": project,
                                   "prompt": "Decisión: Leaflet"})
    assert r.returncode == 0, r.stderr
    e = entries(ledger.Store(project))[-1]
    assert e["text"] == "Decisión: Leaflet" and e["sig"]["key"] == signer["fingerprint"]
    assert signing.verify(ledger.Store(project))["valid"] == 1


def test_removing_or_forging_a_signature_is_detected(project, signer, tmp_path):
    harness.init_store(project)
    store = ledger.Store(project)
    ledger.write_note(store, "#idea mine")
    # stripping the signature changes the entry, so the chain breaks
    lines = open(store.ledger, encoding="utf-8").read().splitlines()
    e = json.loads(lines[0])
    del e["sig"]
    open(store.ledger, "w", encoding="utf-8").write(ledger.canonical_json(e) + "\n")
    assert not ledger.verify(store)["ok"]
    # re-signing with another key and re-hashing keeps the chain, but the key is not the inventor's
    other = make_key(tmp_path, "other")
    pub, fp = signing.public_key(other)
    e["sig"] = {"ns": "authorship", "principal": "inventor@example.com", "key": fp,
                "sshsig": signing.sign_bytes(other, signing.message(e))}
    e["hash"] = ledger.entry_hash(e)
    open(store.ledger, "w", encoding="utf-8").write(ledger.canonical_json(e) + "\n")
    ledger._write_json_atomic(store.head, {"seq": 1, "hash": e["hash"]})
    assert ledger.verify(store)["ok"]  # the chain alone cannot tell
    rep = signing.verify(store)
    assert rep["invalid"] == [1] and not rep["ok"]  # not in allowed_signers under that principal
    # changing the signed text after the fact
    e2 = dict(e, text="#idea someone else's")
    assert signing.verify(store, entries=[e2])["invalid"] == [1]


def test_no_setup_no_signature_and_failure_never_blocks(project, tmp_path, monkeypatch):
    harness.init_store(project)
    store = ledger.Store(project)
    ledger.write_note(store, "#idea unsigned")
    assert "sig" not in entries(store)[0]
    signing.save_config({"key": str(tmp_path / "missing"), "pub": "ssh-ed25519 AAAA", "fingerprint": "SHA256:x",
                         "principal": "p"})
    ledger.write_note(store, "#idea still recorded")
    assert len(entries(store)) == 2 and "sig" not in entries(store)[1]
    assert "signing" in open(os.path.join(store.root, "errors.log")).read()


def test_sign_command(project, tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "spawned_by_claude", lambda: False)
    key = make_key(tmp_path)
    out = io.StringIO()
    assert cli.cmd_sign(["setup", "--key", key, "--principal", "me@example.com"], stdout=out) == 0
    assert "SHA256:" in out.getvalue() and signing.config()["principal"] == "me@example.com"
    out = io.StringIO()
    assert cli.cmd_sign(["status"], stdout=out) == 0 and "on, key SHA256:" in out.getvalue()
    assert cli.cmd_sign(["off"], stdout=io.StringIO()) == 0 and signing.config() is None
    assert cli.cmd_sign(["setup", "--key", str(tmp_path / "nope")], stdout=io.StringIO()) == 1


def test_sign_refuses_claude(project):
    r = subprocess.run([__import__("sys").executable, os.path.join(harness.SCRIPTS, "cli.py"), "sign", "setup"],
                       capture_output=True, text=True, env=harness.hook_env(project, CLAUDECODE="1"), timeout=30)
    assert r.returncode == 3 and "human-only" in r.stderr
