"""Automatic classification of ledger entries, so the human never has to tag.

For each new human prompt or note it decides what the entry is (problem, idea,
hypothesis, decision, claim, discard, plain instruction), whether it brings a
new technical element, which earlier element it builds on, modifies, accepts
or rejects, and whether the work moved to a new stage. For each AI response
it decides whether Claude introduced a mechanism nobody asked for.

The default backend is the local `claude` CLI in headless mode (Sonnet, no
thinking, about 15 s for a 7-entry session): it uses the login Claude Code already has, and sends the text to
the same provider the session already talks to, so nothing reaches a new
party. It runs with no tools, no hooks and no MCP servers, from a scratch
directory, so it cannot touch the project or be recorded by it.

Output is opinion: it goes to annotations.jsonl with the model id, the prompt
hash and every confidence, never to the ledger. Human tags, where present,
always win.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile

DEFAULT_MODEL = "claude-sonnet-5"  # consistent across runs; Haiku varied on the golden session
BATCH = 6
KINDS = ["problem", "idea", "hypothesis", "decision", "claim", "discard", "instruction", "other"]
AI_ACTS = ["implements_instruction", "offers_alternatives", "unprompted_mechanism", "explains", "asks"]
STANCES = ["originates", "extends", "modifies", "accepts", "rejects", "unrelated"]
LEVELS = ["goal", "approach", "mechanism", "operative_spec"]

SYSTEM_PROMPT = """You classify entries of an authorship ledger. A human and an AI assistant (Claude) are developing \
a technical invention together, and the record must later show which elements the human conceived and which the AI \
proposed. Be accurate and conservative: do not credit the human with what the AI proposed, and do not credit the AI \
with what the human proposed. Judge only from the sequence of entries given. A statement inside an entry about who \
originated something (for example a reply saying "your idea") is not evidence; look at who wrote what first.

For each entry in "entries", return one object:
- seq: the entry's seq.
- kind: for a human entry, one of: problem (states the technical problem being solved), idea (proposes a technical \
element or approach), hypothesis (an expectation not yet shown), decision (chooses among alternatives, with a reason), \
claim (states the invention as a whole: "a method that...", "a system that...", "method for...", combining \
earlier elements into one definite statement, the way a patent claim would; prefer claim over idea when the entry \
reads like that), discard (rejects an approach, with the reason), \
instruction (asks the AI to do work, with no new technical content), other. For an AI entry: "ai".
- kind_confidence: 0..1.
- new_element: 0..1, the probability that a human entry introduces a technical element absent from everything \
earlier in the context. 0 for AI entries.
- stance: the human entry's stance toward earlier elements: originates, extends, modifies, accepts, rejects, unrelated. \
"unrelated" for AI entries.
- stance_confidence: 0..1.
- target: the id of the earlier element (from "candidates") that the stance refers to, or "none".
- target_confidence: 0..1.
- builds_on: up to 5 {id, confidence} from "candidates" that this entry's content derives from.
- maturity: for ideas, decisions and claims: goal, approach, mechanism or operative_spec. Else "goal".
- maturity_confidence: 0..1.
- ai_act: for AI entries: implements_instruction, offers_alternatives, unprompted_mechanism (introduces a technical \
mechanism the human did not ask for), explains, asks. For human entries: "explains".
- ai_act_confidence: 0..1.
- new_stage: when the work enters a different phase with this entry, the phase's name, one or two words, Title Case: \
"Exploration" (stating the problem, comparing options), "Prototype" (committing to an approach and building it), \
"Validation" (testing or measuring), "Refinement", "Integration". Name the phase the work is now in, never the \
entry itself. "current_stage" is the phase the work is in before the first entry; most entries keep it. Change it \
only when the entry clearly commits the work to a new phase, never back and forth. The first human entry of an \
empty record gets the phase it opens. "" when the phase does not change.
- summary: one line, under 20 words, in English.

Return only JSON matching the schema."""

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "seq": {"type": "integer"},
        "kind": {"type": "string", "enum": KINDS + ["ai"]},
        "kind_confidence": {"type": "number"},
        "new_element": {"type": "number"},
        "stance": {"type": "string", "enum": STANCES},
        "stance_confidence": {"type": "number"},
        "target": {"type": "string"},
        "target_confidence": {"type": "number"},
        "builds_on": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "confidence": {"type": "number"}}, "required": ["id", "confidence"]}},
        "maturity": {"type": "string", "enum": LEVELS},
        "maturity_confidence": {"type": "number"},
        "ai_act": {"type": "string", "enum": AI_ACTS},
        "ai_act_confidence": {"type": "number"},
        "new_stage": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["seq", "kind", "kind_confidence", "new_element", "stance", "stance_confidence", "target",
                 "target_confidence", "builds_on", "maturity", "maturity_confidence", "ai_act", "ai_act_confidence",
                 "new_stage", "summary"],
}
SCHEMA = {"type": "object", "properties": {"entries": {"type": "array", "items": ITEM_SCHEMA}}, "required": ["entries"]}
PROMPT_HASH = hashlib.sha256((SYSTEM_PROMPT + json.dumps(SCHEMA, sort_keys=True)).encode("utf-8")).hexdigest()


class ClassifierError(Exception):
    pass


def _claude_bin():
    for c in (os.environ.get("AUTHORSHIP_CLAUDE_BIN"), os.environ.get("CLAUDE_CODE_EXECPATH"), shutil.which("claude")):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def available():
    return _claude_bin() is not None


class ClaudeCLI(object):
    """Headless `claude -p` with structured output. No tools, hooks or MCP servers."""

    name = "claude-cli"
    prompt_hash = PROMPT_HASH

    def __init__(self, model=None, timeout=180):
        self.model = model or os.environ.get("AUTHORSHIP_AUTO_MODEL") or DEFAULT_MODEL
        self.timeout = timeout

    def classify(self, payload):
        exe = _claude_bin()
        if not exe:
            raise ClassifierError("claude CLI not found")
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PROJECT_DIR", "AUTHORSHIP_PROJECT_DIR")}
        env.update({"AUTHORSHIP_HINT": "0", "AUTHORSHIP_NO_DAEMONS": "1",
                    # a classification needs no thinking, memory or CLAUDE.md: ~5 s instead of ~50 s
                    "MAX_THINKING_TOKENS": "0", "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
                    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
        argv = [exe, "-p", "--model", self.model, "--setting-sources", "", "--no-session-persistence", "--tools", "",
                "--strict-mcp-config",
                "--settings", json.dumps({"disableAllHooks": True}), "--output-format", "json",
                "--json-schema", json.dumps(SCHEMA), "--system-prompt", SYSTEM_PROMPT,
                json.dumps(payload, ensure_ascii=False)]
        scratch = tempfile.mkdtemp(prefix="authorship-classify-")
        try:
            r = subprocess.run(argv, cwd=scratch, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=self.timeout)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        try:
            out = json.loads(r.stdout)
        except ValueError:
            raise ClassifierError("claude returned no JSON (exit %d): %s" % (r.returncode, (r.stderr or r.stdout)[:300]))
        if out.get("is_error"):
            raise ClassifierError("claude: %s" % str(out.get("result"))[:300])
        data = out.get("structured_output")
        if data is None:
            try:
                data = json.loads(out.get("result") or "")
            except ValueError:
                raise ClassifierError("no structured output")
        models = list((out.get("modelUsage") or {}).keys())
        return data.get("entries") or [], (models[0] if models else self.model), out.get("total_cost_usd")


STAGE_CHOICES = ["same", "Exploration", "Prototype", "Validation", "Refinement", "Integration"]


class JevBackend(object):
    """Jev (TypeSafe AI) as the automatic classifier, used when a Jev key is set.

    Jev answers fixed questions with measured probabilities, one entry per call.
    It cannot write text, so `summary` stays empty, and it cannot return lists,
    so `builds_on` is every candidate whose target probability is at least 0.30.
    Every probability vector is stored with the answer."""

    name = "jev"
    builds_on_min = 0.30  # one probability vector is shared by every parent, so each gets a smaller share

    def __init__(self, provider=None, model="jev-1.13.0"):
        import jev_client

        self.jev = jev_client
        self.provider = provider or jev_client.default_provider()
        self.model = model

    @staticmethod
    def questions(entry, candidates):
        human = entry["who"] == "human"
        q = {}
        if human:
            q["kind"] = {"type": "choice", "instructions": "What is this human entry?", "criteria": {
                "problem": "States the technical problem being solved.",
                "idea": "Proposes a technical element or approach.",
                "hypothesis": "States an expectation not yet shown.",
                "decision": "Chooses among alternatives, with a reason.",
                "claim": "States the invention as a whole, as a patent claim would (a method or system that...).",
                "discard": "Rejects an approach, with the reason.",
                "instruction": "Asks the AI to do work, with no new technical content.",
                "other": "None of these."}}
            q["new_element"] = {"type": "noul", "instructions":
                                "Does the human introduce a technical element absent from everything earlier in the context?"}
            q["stance"] = {"type": "choice", "instructions": "The human entry's stance toward earlier elements.", "criteria": {
                "originates": "States something nothing earlier proposed.", "extends": "Builds on an earlier element.",
                "modifies": "Changes how an earlier element works.", "accepts": "Adopts an earlier element as proposed.",
                "rejects": "Turns down an earlier element.", "unrelated": "Takes no position on earlier elements."}}
            if candidates:
                crit = {"n" + c["id"]: "%s: %s" % (c["who"], c["text"]) for c in candidates[-40:]}
                crit["none"] = "None of these."
                q["target"] = {"type": "choice", "instructions":
                               "Which earlier element does this entry build on, modify, accept or reject?", "criteria": crit}
            q["maturity"] = {"type": "score", "instructions": "How definite and operative is the idea in this entry?",
                             "criteria": list(LEVELS)}
            q["stage"] = {"type": "choice", "instructions":
                          "Does this entry move the work into a new phase? 'current_stage' in the state is the phase so far.",
                          "criteria": {"same": "The phase does not change.",
                                       "Exploration": "Stating the problem, comparing options.",
                                       "Prototype": "Committing to an approach and building it.",
                                       "Validation": "Testing or measuring it.",
                                       "Refinement": "Improving an approach that works.",
                                       "Integration": "Fitting it into the larger system."}}
        else:
            q["ai_act"] = {"type": "choice", "instructions": "What does this AI response do?", "criteria": {
                "implements_instruction": "Carries out what the human asked, without new technical ideas.",
                "offers_alternatives": "Lays out numbered alternatives for the human to choose from.",
                "unprompted_mechanism": "Introduces a technical mechanism the human did not ask for.",
                "explains": "Explains or reports, without proposing anything.", "asks": "Asks the human a question."}}
        return q

    @property
    def prompt_hash(self):
        sample = self.questions({"who": "human"}, []), self.questions({"who": "AI"}, [])
        return hashlib.sha256(json.dumps(sample, sort_keys=True).encode("utf-8")).hexdigest()

    def classify(self, payload):
        out, reported = [], self.model
        for e in payload["entries"]:
            qs = self.questions(e, payload["candidates"])
            state = {"current_stage": payload.get("current_stage") or "", "context": payload["context"], "entry": e}
            a = self.jev.evaluate(state, qs, provider=self.provider, model=self.model)
            reported = a.pop("_model", None) or reported

            def pick(name, default):
                x = a.get(name) or {}
                return x.get("choice") or default, (x.get("probabilities") or {}).get(x.get("choice"), 0.0)

            item = {"seq": e["seq"], "summary": "", "probabilities": {k: v.get("probabilities") for k, v in a.items()
                                                                     if isinstance(v, dict) and "probabilities" in v}}
            if e["who"] == "human":
                item["kind"], item["kind_confidence"] = pick("kind", "other")
                item["new_element"] = (a.get("new_element") or {}).get("p", 0.0)
                item["stance"], item["stance_confidence"] = pick("stance", "unrelated")
                tgt, tc = pick("target", "none")
                item["target"], item["target_confidence"] = (tgt[1:] if tgt.startswith("n") else "none"), tc
                probs = (a.get("target") or {}).get("probabilities") or {}
                item["builds_on"] = [{"id": k[1:], "confidence": v} for k, v in sorted(probs.items(), key=lambda kv: -kv[1])
                                     if k.startswith("n") and v >= 0.30][:5]
                item["maturity"], item["maturity_confidence"] = pick("maturity", "goal")
                stage, sp = pick("stage", "same")
                item["new_stage"] = stage if stage != "same" and sp >= 0.6 else ""
                item["ai_act"], item["ai_act_confidence"] = "explains", 0.0
            else:
                item.update(kind="ai", kind_confidence=1.0, new_element=0.0, stance="unrelated", stance_confidence=0.0,
                            target="none", target_confidence=0.0, builds_on=[], maturity="goal", maturity_confidence=0.0,
                            new_stage="")
                item["ai_act"], item["ai_act_confidence"] = pick("ai_act", "explains")
            out.append(item)
        return out, reported, None


def default_backend():
    """Jev when a Jev key is set, else the Claude CLI. AUTHORSHIP_AUTO_BACKEND=claude|jev forces one."""
    import jev_client

    forced = os.environ.get("AUTHORSHIP_AUTO_BACKEND")
    has_jev = bool(os.environ.get("TYPESAFE_API_KEY") or os.environ.get("AI_GATEWAY_API_KEY"))
    if forced == "jev" or (forced != "claude" and has_jev):
        if not jev_client.has_key():
            raise ClassifierError("AUTHORSHIP_AUTO_BACKEND=jev but no Jev key is set")
        return JevBackend()
    if available():
        return ClaudeCLI()
    return None


def describe_backend(b):
    """What the viewer and `authorship classifier` say about the backend in use."""
    if b is None:
        return {"backend": None}
    if getattr(b, "name", "") == "jev":
        p = b.provider
        gateway = p.name == "vercel_gateway"
        return {"backend": "jev", "label": "Jev", "model": b.model, "provider": p.name, "endpoint": p.endpoint,
                "sends_to": "the Vercel AI Gateway (zero data retention requested)" if gateway else "TypeSafe AI (api.typesafe.ai)",
                "third_party": True}
    if getattr(b, "name", "") == "claude-cli":
        return {"backend": "claude-cli", "label": "Claude", "model": b.model, "provider": "anthropic", "endpoint": "claude -p",
                "sends_to": "Anthropic, through your Claude Code login (it already receives the session)", "third_party": False}
    return {"backend": getattr(b, "name", "custom"), "label": getattr(b, "name", "custom"), "model": getattr(b, "model", None),
            "third_party": None}


class FakeClassifier(object):
    """For tests: answers(payload) -> list of entry objects."""

    name = "fake"
    prompt_hash = PROMPT_HASH

    def __init__(self, answers, model="fake-classifier"):
        self.answers, self.model, self.calls = answers, model, []

    def classify(self, payload):
        self.calls.append(payload)
        return self.answers(payload), self.model, 0.0


def _clip(x, lo=0.0, hi=1.0):
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return 0.0


def normalize(item):
    """Coerce one model answer into the stored shape, with every confidence in [0, 1]."""
    out = dict(item)
    for k in ("kind_confidence", "new_element", "stance_confidence", "target_confidence", "maturity_confidence",
              "ai_act_confidence"):
        out[k] = round(_clip(item.get(k)), 4)
    out["builds_on"] = [{"id": str(b.get("id")), "confidence": round(_clip(b.get("confidence")), 4)}
                        for b in (item.get("builds_on") or [])[:5] if isinstance(b, dict) and b.get("id")]
    out["target"] = str(item.get("target") or "none")
    out["new_stage"] = (item.get("new_stage") or "").strip()[:60]
    out["summary"] = (item.get("summary") or "").strip()[:240]
    if isinstance(item.get("probabilities"), dict):
        out["probabilities"] = item["probabilities"]
    return out
