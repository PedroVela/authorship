"""Deterministic classification helpers shared by the index and the annotator.

Nothing here judges inventorship; it only recognizes structure (tags,
numbered alternatives, "AI proposal:" lines, test commands and their results).
"""
import json
import re

RULES_VERSION = "tier0-rules-v1"

TEST_CMD = re.compile(
    r"(?:^|[\s;&|(/])(?:pytest|py\.test|python3?\s+-m\s+(?:pytest|unittest)|(?:npm|yarn|pnpm|bun)\s+(?:run\s+)?test"
    r"|jest|vitest|mocha|go\s+test|cargo\s+(?:test|nextest)|rspec|mvn\s+(?:\S+\s+)*test|gradlew?\s+test|make\s+(?:check|test)"
    r"|tox|nox|deno\s+test|phpunit|ctest|dotnet\s+test|swift\s+test|mix\s+test)\b"
)
FAILED_OUTPUT = re.compile(r"\b[1-9]\d* (?:failed|failing|errors?)\b|^FAILED\b|Tests:\s+[1-9]\d* failed|\bFAIL\b", re.M)
OPTION_RE = re.compile(r"^[ \t]{0,3}(?:\*\*)?(?:\((\d{1,2})\)|(\d{1,2})[.)])(?:\*\*)?[ \t]+(.+?)[ \t]*$", re.M)
AI_PROPOSAL_RE = re.compile(r"^[ \t>*_-]*(?:\*\*)?AI proposal:(?:\*\*)?[ \t]*(.+?)[ \t]*$", re.M | re.I)
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

# Tag -> (ibis type, tier-0 milestone)
TAG_RULES = [
    ("#claim", "claim", "claim_candidate"),
    ("#decision", "decision", "decision_with_reason"),
    ("#problem", "issue", "problem_fixed"),
    ("#discard", "position", "discard_with_reason"),
    ("#idea", "position", "conception"),
    ("#hypothesis", "position", None),
]
HUMAN_DECLARED = ("claim_candidate", "decision_with_reason", "problem_fixed", "discard_with_reason", "conception",
                  "stage_opened")


def human_ibis(tags):
    for tag, ibis, _ in TAG_RULES:
        if tag in (tags or []):
            return ibis
    return "remark"


def tag_milestones(tags):
    out = [m for tag, _, m in TAG_RULES if m and tag in (tags or [])]
    if "#stage" in (tags or []):
        out.append("stage_opened")
    return out


def parse_options(text):
    """Numbered alternatives 1..k (k >= 2) in one response. Returns [(n, label)]."""
    found = []
    for m in OPTION_RE.finditer(text or ""):
        n = int(m.group(1) or m.group(2))
        found.append((n, m.group(3)))
    # keep the first consecutive run that starts at 1
    run = []
    for n, label in found:
        if n == len(run) + 1:
            run.append((n, label))
        elif run and n == 1:
            if len(run) >= 2:
                break
            run = [(n, label)]
    return run if len(run) >= 2 else []


def parse_ai_proposals(text):
    return [m.group(1) for m in AI_PROPOSAL_RE.finditer(text or "")]


def entry_text(entry, store=None):
    """Full text of an entry: inline text, or the blob when only a preview is inline."""
    if entry.get("text") is not None:
        return entry["text"]
    if entry.get("blob") and store is not None:
        try:
            with open(store.blob_path(entry["blob"]), "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            pass
    return entry.get("preview") or ""


def read_blob_json(store, ref):
    if not isinstance(ref, dict) or not ref.get("blob"):
        return None
    try:
        with open(store.blob_path(ref["blob"]), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def is_test_command(cmd):
    return bool(cmd and TEST_CMD.search(cmd))


def test_result(entry, store=None):
    """'pass' | 'fail' | None for a Bash test run."""
    if entry.get("kind") != "tool" or entry.get("tool") != "Bash" or not is_test_command(entry.get("command")):
        return None
    if entry.get("outcome") in ("failure", "interrupted"):
        return "fail"
    resp = read_blob_json(store, entry.get("response")) if store is not None else None
    if isinstance(resp, dict):
        out = "%s\n%s" % (resp.get("stdout") or "", resp.get("stderr") or "")
        if FAILED_OUTPUT.search(out):
            return "fail"
    return "pass"


def short(text, n=160):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def file_of(entry):
    return entry.get("file") if entry.get("tool") in EDIT_TOOLS else None


def node_seq(node_id):
    return int(str(node_id).split(".")[0])


def is_sub(node_id):
    return "." in str(node_id)
