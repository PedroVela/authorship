"""Provider-agnostic client for Jev (TypeSafe AI's evaluator model).

    evaluate(state, questions, provider=None) -> answers

`questions` uses Jev's wire format ({name: {type, instructions, criteria}}).
Answers are normalized to:
    noul   -> {"type": "noul", "p": 0.93}
    choice -> {"type": "choice", "choice": "modifies", "probabilities": {...}}
    score  -> {"type": "score", "score": 2.1, "choice": "mechanism", "probabilities": {...}}
plus "_model" (the model id the provider reports).

Providers:
    typesafe        POST https://api.typesafe.ai/v1/systemone          (TYPESAFE_API_KEY)
    openrouter      POST https://openrouter.ai/api/alpha/decisions     (OPENROUTER_API_KEY), zero data retention
    vercel_gateway  POST https://ai-gateway.vercel.sh/v1/evaluate      (AI_GATEWAY_API_KEY), zero data retention
    fake            fixed outputs, for tests

Standard library only. Nothing here runs unless AUTHORSHIP_JEV=1.
"""
import json
import os
import urllib.request

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
GATEWAY_URL = "https://ai-gateway.vercel.sh/v1/evaluate"
OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
MAX_CHOICE_OPTIONS = 255


class JevError(Exception):
    pass


def _post(url, key, body, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # urllib raises several types
        raise JevError("%s: %s" % (url, exc))


def _normalize(raw_answers, questions):
    out = {}
    for name, q in questions.items():
        a = (raw_answers or {}).get(name)
        if not isinstance(a, dict):
            continue
        t = q.get("type")
        if t == "noul":
            p = a.get("noul", a.get("probability"))
            if p is not None:
                out[name] = {"type": "noul", "p": float(p)}
        elif t == "choice":
            probs = {k: float(v) for k, v in (a.get("probabilities") or {}).items()}
            choice = a.get("choice") or (max(probs, key=probs.get) if probs else None)
            out[name] = {"type": "choice", "choice": choice, "probabilities": probs}
        elif t == "score":
            levels = q.get("criteria") or []
            probs = {}
            for k, v in (a.get("probabilities") or {}).items():
                try:
                    probs[levels[int(k)]] = float(v)
                except (ValueError, IndexError):
                    probs[str(k)] = float(v)
            score = float(a.get("score", 0.0))
            idx = max(0, min(len(levels) - 1, int(round(score)))) if levels else 0
            out[name] = {"type": "score", "score": score, "choice": levels[idx] if levels else None,
                         "probabilities": probs}
    return out


class TypesafeProvider(object):
    name = "typesafe"

    def __init__(self, key=None, url=None):
        self.key = key or os.environ.get("TYPESAFE_API_KEY")
        self.url = url or os.environ.get("AUTHORSHIP_JEV_ENDPOINT") or TYPESAFE_URL

    @property
    def endpoint(self):
        return self.url

    def call(self, state, questions, model):
        if not self.key:
            raise JevError("TYPESAFE_API_KEY is not set")
        res = _post(self.url, self.key, {"state": state, "model": model, "questions": questions})
        return res.get("answers") or {}, res.get("model") or model


def openrouter_model(model):
    """jev-1.13.0 -> typesafe/jev-1.13 (OpenRouter pins major.minor and answers with a dated snapshot)."""
    if model and model.startswith("typesafe/"):
        return model
    parts = (model or "jev-1.13.0").split(".")
    return "typesafe/" + ".".join(parts[:2])


class OpenRouterProvider(object):
    """OpenRouter's Decisions API, routed to TypeSafe. Same question and answer format as TypeSafe's own
    API. Asks for zero-data-retention endpoints only, no data collection, and no fallback provider."""
    name = "openrouter"

    def __init__(self, key=None, url=None):
        self.key = key or os.environ.get("OPENROUTER_API_KEY")
        self.url = url or os.environ.get("AUTHORSHIP_JEV_ENDPOINT") or OPENROUTER_URL

    @property
    def endpoint(self):
        return self.url

    def call(self, state, questions, model):
        if not self.key:
            raise JevError("OPENROUTER_API_KEY is not set")
        body = {"model": openrouter_model(model), "state": state, "questions": questions,
                "provider": {"zdr": True, "data_collection": "deny", "allow_fallbacks": False}}
        res = _post(self.url, self.key, body)
        return res.get("answers") or {}, res.get("model") or body["model"]


class VercelGatewayProvider(object):
    """Vercel AI Gateway. Its noul type is called "boolean" and it cannot pin a Jev version."""
    name = "vercel_gateway"

    def __init__(self, key=None, url=None):
        self.key = key or os.environ.get("AI_GATEWAY_API_KEY")
        self.url = url or os.environ.get("AUTHORSHIP_JEV_ENDPOINT") or GATEWAY_URL

    @property
    def endpoint(self):
        return self.url

    def call(self, state, questions, model):
        if not self.key:
            raise JevError("AI_GATEWAY_API_KEY is not set")
        wire = {}
        for n, q in questions.items():
            q = dict(q)
            if q.get("type") == "noul":
                q["type"] = "boolean"
            wire[n] = q
        body = {"model": "typesafe-ai/jev", "state": state, "questions": wire,
                "providerOptions": {"gateway": {"zeroDataRetention": True}}}
        res = _post(self.url, self.key, body)
        return res.get("answers") or {}, res.get("model") or "typesafe-ai/jev"


class FakeProvider(object):
    """answers: dict {question: raw answer}, or callable(state, questions) -> dict."""
    name = "fake"
    endpoint = "fake://jev"

    def __init__(self, answers, model="jev-1.13.0"):
        self.answers, self.model, self.calls = answers, model, []

    def call(self, state, questions, model):
        self.calls.append((state, questions))
        a = self.answers(state, questions) if callable(self.answers) else self.answers
        return a, self.model


def default_provider():
    """AUTHORSHIP_JEV_PROVIDER when set; otherwise whichever key is present: TypeSafe, then OpenRouter,
    then the Vercel AI Gateway."""
    name = os.environ.get("AUTHORSHIP_JEV_PROVIDER")
    if not name:
        name = "typesafe"
        for env_key, provider in (("TYPESAFE_API_KEY", "typesafe"), ("OPENROUTER_API_KEY", "openrouter"),
                                  ("AI_GATEWAY_API_KEY", "vercel_gateway")):
            if os.environ.get(env_key):
                name = provider
                break
    if name == "openrouter":
        return OpenRouterProvider()
    if name == "vercel_gateway":
        return VercelGatewayProvider()
    if name == "typesafe":
        return TypesafeProvider()
    raise JevError("unknown AUTHORSHIP_JEV_PROVIDER %r" % name)


def has_key(provider=None):
    p = provider or default_provider()
    return bool(getattr(p, "key", None)) or p.name == "fake"


def evaluate(state, questions, provider=None, model="jev-1.13.0"):
    for n, q in questions.items():
        if q.get("type") == "choice" and len(q.get("criteria") or {}) > MAX_CHOICE_OPTIONS:
            raise JevError("question %s has more than %d options" % (n, MAX_CHOICE_OPTIONS))
    provider = provider or default_provider()
    raw, reported = provider.call(state, questions, model)
    answers = _normalize(raw, questions)
    answers["_model"] = reported
    return answers
