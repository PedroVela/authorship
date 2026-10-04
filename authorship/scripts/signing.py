"""SSH signatures on the human's entries: who wrote them, not only what and when.

    authorship sign setup [--key PATH]   choose the key (human-only); test-signs once
    authorship sign off                  stop signing
    authorship sign status               the key in use and how many entries carry a signature

When set up, every human entry (prompt, note, confirmation) gets a field

    "sig": {"ns": "authorship", "principal": "<email>", "key": "SHA256:...", "sshsig": "-----BEGIN SSH SIGNATURE-----..."}

over message(entry): canonical JSON of the entry without "hash" and "sig". The
signature is inside the entry, so the entry hash covers it: removing or
changing it breaks the chain. `ssh-keygen -Y verify` checks it against
.authorship/allowed_signers, which lists each public key used.

A signature proves the entry was made where the private key was. The
allowed_signers file sits next to the ledger, so the key must also be
recognized from outside it: compare its fingerprint with the one published for
you (for example https://github.com/<user>.keys) or held by your attorney.
"""
import json
import os
import shutil
import subprocess
import tempfile

NAMESPACE = "authorship"
SIGN_TIMEOUT = 10


def config_path():
    return os.environ.get("AUTHORSHIP_SIGNING_CONFIG") or os.path.join(
        os.path.expanduser("~"), ".config", "authorship", "signing.json")


def config():
    try:
        with open(config_path(), encoding="utf-8") as f:
            c = json.load(f)
        return c if isinstance(c, dict) and c.get("key") else None
    except (OSError, ValueError):
        return None


def save_config(c):
    path = config_path()
    if not c:
        try:
            os.remove(path)
        except OSError:
            pass
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2, sort_keys=True)
        f.write("\n")
    return path


def ssh_keygen():
    return os.environ.get("AUTHORSHIP_SSH_KEYGEN") or shutil.which("ssh-keygen")


def message(entry):
    import ledger

    return ledger.canonical_json({k: v for k, v in entry.items() if k not in ("hash", "sig")}).encode("utf-8")


def _run(argv, data=None, timeout=SIGN_TIMEOUT):
    return subprocess.run(argv, input=data, stdin=None if data is not None else subprocess.DEVNULL,
                          capture_output=True, timeout=timeout)


def public_key(key):
    """(public key line 'type base64', fingerprint 'SHA256:...')."""
    pub = key + ".pub"
    if os.path.exists(pub):
        with open(pub, encoding="utf-8") as f:
            line = f.read().split("\n")[0].strip()
    else:
        r = _run([ssh_keygen(), "-y", "-f", key])
        if r.returncode != 0:
            raise RuntimeError("cannot read the public key of %s: %s" % (key, r.stderr.decode(errors="replace").strip()))
        line = r.stdout.decode().strip()
    parts = line.split()
    with tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False) as f:
        f.write(" ".join(parts[:2]) + "\n")
        tmp = f.name
    try:
        r = _run([ssh_keygen(), "-l", "-E", "sha256", "-f", tmp])
    finally:
        os.remove(tmp)
    if r.returncode != 0:
        raise RuntimeError("not an SSH public key: %s" % line[:40])
    return " ".join(parts[:2]), r.stdout.decode().split()[1]


def sign_bytes(key, data):
    """The armored SSH signature of data, or raises. Needs a key without passphrase, or one in ssh-agent."""
    exe = ssh_keygen()
    if not exe:
        raise RuntimeError("ssh-keygen not found")
    with tempfile.TemporaryDirectory(prefix="authorship-sign-") as d:
        msg = os.path.join(d, "m")
        with open(msg, "wb") as f:
            f.write(data)
        r = _run([exe, "-Y", "sign", "-f", key, "-n", NAMESPACE, msg])
        if r.returncode != 0 or not os.path.exists(msg + ".sig"):
            raise RuntimeError("ssh-keygen could not sign (a passphrase-protected key needs ssh-agent): %s"
                               % r.stderr.decode(errors="replace").strip()[:300])
        with open(msg + ".sig", encoding="utf-8") as f:
            return f.read()


def verify_bytes(allowed_signers, principal, sshsig, data):
    exe = ssh_keygen()
    if not exe:
        return None  # cannot check
    with tempfile.TemporaryDirectory(prefix="authorship-verify-") as d:
        sig = os.path.join(d, "s.sig")
        with open(sig, "w", encoding="utf-8") as f:
            f.write(sshsig)
        r = _run([exe, "-Y", "verify", "-f", allowed_signers, "-I", principal, "-n", NAMESPACE, "-s", sig], data=data)
        return r.returncode == 0


def allowed_signers_path(store):
    return os.path.join(store.root, "allowed_signers")


def _ensure_allowed(store, principal, pub):
    path = allowed_signers_path(store)
    line = "%s namespaces=\"%s\" %s" % (principal, NAMESPACE, pub)
    try:
        with open(path, encoding="utf-8") as f:
            if line in f.read().splitlines():
                return
    except OSError:
        pass
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def sign_entry(store, entry):
    """Add entry["sig"] when signing is set up. Called by ledger.append, before hashing. Never raises."""
    c = config()
    if not c or entry.get("actor") != "human":
        return
    try:
        _ensure_allowed(store, c["principal"], c["pub"])
        entry["sig"] = {"ns": NAMESPACE, "principal": c["principal"], "key": c["fingerprint"],
                        "sshsig": sign_bytes(os.path.expanduser(c["key"]), message(entry))}
    except Exception as exc:
        store.log_error("signing", exc)


def verify(store, entries=None):
    """Signature report for the human entries."""
    import ledger

    entries = entries if entries is not None else [e for _, _, e in ledger.read_entries(store) if e]
    allowed = allowed_signers_path(store)
    human = [e for e in entries if e.get("actor") == "human"]
    signed = [e for e in human if isinstance(e.get("sig"), dict)]
    out = {"human": len(human), "signed": len(signed), "valid": 0, "invalid": [], "unchecked": 0, "keys": {}}
    for e in signed:
        s = e["sig"]
        out["keys"][s.get("key")] = s.get("principal")
        ok = verify_bytes(allowed, s.get("principal") or "", s.get("sshsig") or "", message(e)) \
            if os.path.exists(allowed) else False
        if ok is None:
            out["unchecked"] += 1
        elif ok:
            out["valid"] += 1
        else:
            out["invalid"].append(e["seq"])
    out["ok"] = not out["invalid"]
    return out


def describe(report):
    if not report["signed"]:
        return "signatures: none (%d human entries; `authorship sign setup` to sign new ones)" % report["human"]
    keys = ", ".join("%s (%s)" % (k, p) for k, p in sorted(report["keys"].items()))
    state = ("all valid" if report["ok"] and not report["unchecked"] else
             "%d unchecked (no ssh-keygen)" % report["unchecked"] if report["ok"] else
             "INVALID at #%s" % ", #".join(str(s) for s in report["invalid"]))
    return "signatures: %d of %d human entries signed by %s; %s" % (report["signed"], report["human"], keys, state)


def default_key():
    """git's SSH signing key, else ~/.ssh/id_ed25519, else None."""
    try:
        r = _run(["git", "config", "--get", "gpg.format"])
        if r.stdout.decode().strip() == "ssh":
            k = _run(["git", "config", "--get", "user.signingkey"]).stdout.decode().strip()
            if k and os.path.exists(os.path.expanduser(k)):
                return os.path.expanduser(k[:-4] if k.endswith(".pub") else k)
    except (OSError, subprocess.SubprocessError):
        pass
    k = os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519")
    return k if os.path.exists(k) else None


def default_principal():
    try:
        e = _run(["git", "config", "--get", "user.email"]).stdout.decode().strip()
        if e:
            return e
    except (OSError, subprocess.SubprocessError):
        pass
    import ledger

    return ledger.default_author()


def setup(key=None, principal=None):
    """Check that the key signs, then save it. Returns the saved config."""
    key = os.path.abspath(os.path.expanduser(key)) if key else default_key()
    if not key or not os.path.exists(key):
        raise RuntimeError("no SSH key found; create one with `ssh-keygen -t ed25519` or pass --key PATH")
    if key.endswith(".pub"):
        key = key[:-4]
    pub, fp = public_key(key)
    principal = principal or default_principal()
    sig = sign_bytes(key, b"authorship signing test")
    with tempfile.TemporaryDirectory() as d:
        allowed = os.path.join(d, "allowed")
        with open(allowed, "w", encoding="utf-8") as f:
            f.write("%s namespaces=\"%s\" %s\n" % (principal, NAMESPACE, pub))
        if verify_bytes(allowed, principal, sig, b"authorship signing test") is False:
            raise RuntimeError("the test signature did not verify")
    c = {"key": key, "pub": pub, "fingerprint": fp, "principal": principal}
    save_config(c)
    return c
